"""Small native tool-calling Agent, using the existing Bailian connection."""

import asyncio
from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from agentcheck_biz.events import EventLog
from agentcheck_biz.faults import OutcomeUnknown, PermissionDenied, TemporaryUnavailable
from agentcheck_biz.reports import save_json
from .fixed_service import IdempotencyConflict
from .tool_executor import TicketToolExecutor


SYSTEM_PROMPT = """你是维修工单助手，请使用工具处理用户的维修请求。
工具已绑定当前租户及本次业务操作编号，你不需要也不能选择这些编号。
遇到结果未知时，你可以查询核实、重试，或停止并说明需要确认。不要编造工单编号。
只根据工具提供的信息报告结果。最终回答必须是一个 JSON 对象，不要加 Markdown：
{"status":"completed 或 blocked 或 needs_verification","ticket_id":"实际工单编号或 null","summary":"简短中文说明"}。
只有有工具结果支持完成时才使用 completed。权限拒绝属于不可重试错误，停止并在最终 JSON 中增加
"reason":"permission_denied"；暂时不可用允许有限重试。"""

TOOLS = [
    {"type": "function", "function": {
        "name": "create_ticket", "description": "为当前业务请求创建维修工单。结果未知时不能据此断定业务失败。",
        "parameters": {"type": "object", "properties": {
            "customer_id": {"type": "string"}, "device_id": {"type": "string"},
            "description": {"type": "string"}},
            "required": ["customer_id", "device_id", "description"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "query_tickets", "description": "查询当前租户、本次业务请求已保存的全部工单。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
]


@dataclass(frozen=True)
class ModelConfig:
    model: str = "qwen3.7-max"
    max_model_calls: int = 5
    max_output_tokens: int = 512
    timeout_seconds: float = 90
    request_timeout_seconds: float = 30
    temperature: float = 0

    def __post_init__(self):
        if not self.model.strip():
            raise ValueError("model cannot be empty")
        for name in ("max_model_calls", "max_output_tokens"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("timeout_seconds", "request_timeout_seconds"):
            if not 0 < getattr(self, name) <= 300:
                raise ValueError(f"{name} must be between 0 and 300 seconds")


class ModelInvocationError(RuntimeError):
    """Safe provider diagnostics without response bodies or credentials."""


class ModelRunTimedOut(TimeoutError):
    pass


class LiveTicketAgent:
    def __init__(self, config: ModelConfig):
        self.config = config
        self.metadata = {
            **asdict(config), "provider": "bailian", "sdk_retries": 0,
            "enable_thinking": False, "model_request_attempts": 0, "model_responses": 0,
            "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
            "tools_sha256": hashlib.sha256(json.dumps(TOOLS, sort_keys=True).encode("utf-8")).hexdigest(),
            "usage": None, "usage_complete": False,
        }

    def run(self, task: str, executor: TicketToolExecutor, events: EventLog, run_dir: Path) -> dict:
        return asyncio.run(self.run_async(task, executor, events, run_dir))

    async def run_async(self, task, executor, events, run_dir, *, client=None) -> dict:
        """An injected client is only used in explicitly marked offline tests."""
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": task}]
        trace = {"mode": "offline mock" if client is not None else "live model",
                 "tools": deepcopy(TOOLS), "initial_messages": deepcopy(messages), "steps": []}
        self.metadata["mode"] = trace["mode"]
        started = time.monotonic()
        loop_timeout = self.config.timeout_seconds
        run_context = getattr(executor, "run_context", None)
        if run_context is not None:
            loop_timeout = min(loop_timeout, max(0, run_context.deadline - time.time()))
        deadline = started + loop_timeout
        usage_records = []

        def persist():
            self.metadata["elapsed_seconds"] = round(time.monotonic() - started, 3)
            complete = (len(usage_records) == self.metadata["model_request_attempts"]
                        and bool(usage_records) and all(item is not None for item in usage_records))
            self.metadata["usage_complete"] = complete
            self.metadata["usage"] = ({key: sum(item[key] for item in usage_records)
                                       for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
                                      if complete else None)
            save_json(run_dir / "model.json", self.metadata)
            save_json(run_dir / "trajectory.json", trace)

        async def loop(model_client):
            for turn in range(1, self.config.max_model_calls + 1):
                if time.monotonic() >= deadline:
                    raise ModelRunTimedOut("Model loop deadline exceeded")
                self.metadata["model_request_attempts"] += 1
                step = {"turn": turn, "request_messages": deepcopy(messages), "tool_results": []}
                trace["steps"].append(step)
                events.record("model_request_started", turn=turn, model=self.config.model)
                persist()
                try:
                    response = await model_client.chat.completions.create(
                        model=self.config.model, messages=deepcopy(messages), tools=TOOLS,
                        temperature=self.config.temperature, max_tokens=self.config.max_output_tokens,
                        extra_body={"enable_thinking": False},
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # Provider messages and HTTP bodies can contain sensitive data.
                    detail = f"{type(error).__name__}; HTTP status={getattr(error, 'status_code', None)}"
                    events.record("model_request_error", turn=turn, detail=detail)
                    raise ModelInvocationError(detail) from None
                self.metadata["model_responses"] += 1
                choice = response.choices[0]
                message = choice.message
                tool_calls = [{"id": call.id, "type": "function", "function": {
                    "name": call.function.name, "arguments": call.function.arguments}}
                    for call in (message.tool_calls or [])]
                assistant_message = {"role": "assistant", "content": message.content}
                if tool_calls:
                    assistant_message["tool_calls"] = tool_calls
                usage = getattr(response, "usage", None)
                token_usage = ({key: getattr(usage, key, None) for key in (
                    "prompt_tokens", "completion_tokens", "total_tokens")} if usage is not None else None)
                if token_usage and any(value is None for value in token_usage.values()):
                    token_usage = None
                usage_records.append(token_usage)
                step.update(response=assistant_message, response_model=response.model,
                            finish_reason=choice.finish_reason, usage=token_usage)
                events.record("model_response_received", turn=turn, response_model=response.model,
                              tool_call_ids=[call["id"] for call in tool_calls], usage=token_usage)
                messages.append(assistant_message)
                persist()
                if not tool_calls:
                    content = message.content or ""
                    try:
                        result = json.loads(content)
                    except (json.JSONDecodeError, TypeError):
                        result = {"status": "invalid_response", "ticket_id": None, "raw_answer": content}
                    if not isinstance(result, dict):
                        result = {"status": "invalid_response", "ticket_id": None, "raw_answer": content}
                    events.record("client_final_answer", result=result)
                    return result
                for call in tool_calls:
                    if time.monotonic() >= deadline:
                        raise ModelRunTimedOut("Deadline reached before tool execution")
                    try:
                        arguments = json.loads(call["function"]["arguments"])
                        output = executor.execute_tool(call["function"]["name"], arguments, call["id"])
                        tool_result = {"status": "ok", "result": output}
                    except OutcomeUnknown:
                        tool_result = {"status": "outcome_unknown", "message": "Tool response unavailable; outcome unknown"}
                    except TemporaryUnavailable:
                        tool_result = {"status": "temporarily_unavailable", "retryable": True,
                                       "message": "Service temporarily unavailable before execution"}
                    except PermissionDenied:
                        tool_result = {"status": "permission_denied", "retryable": False,
                                       "message": "Permission denied; stop this request"}
                    except IdempotencyConflict:
                        tool_result = {"status": "conflict", "message": "Operation ID already used with different content"}
                    except (ValueError, TypeError):
                        tool_result = {"status": "invalid_arguments", "message": "Tool name or arguments do not match the schema"}
                    # Unexpected service errors and budget exhaustion propagate to the runner.
                    tool_message = {"role": "tool", "tool_call_id": call["id"],
                                    "content": json.dumps(tool_result, ensure_ascii=False)}
                    messages.append(tool_message)
                    step["tool_results"].append(tool_message)
                    events.record("model_tool_result", model_tool_call_id=call["id"], result=tool_result)
                    persist()
            events.record("model_budget_exceeded", limit=self.config.max_model_calls)
            return {"status": "needs_verification", "ticket_id": None, "summary": "Model request budget exhausted"}

        try:
            if time.monotonic() >= deadline:
                raise ModelRunTimedOut("Run deadline reached before model execution")
            if client is not None:
                return await asyncio.wait_for(loop(client), timeout=max(0, deadline - time.monotonic()))
            # Credentials are loaded only at the explicit live entrypoint.
            from openai import AsyncOpenAI, __version__ as sdk_version
            from pipeline.bailian import bailian_connection
            key, base_url = bailian_connection()
            endpoint = urlsplit(base_url)
            if endpoint.scheme != "https" or endpoint.username or endpoint.password or endpoint.query:
                raise ValueError("Bailian endpoint must use HTTPS without URL credentials or query")
            self.metadata.update(endpoint_host=endpoint.hostname, sdk_version=sdk_version)
            async with AsyncOpenAI(api_key=key, base_url=base_url, max_retries=0,
                                   timeout=self.config.request_timeout_seconds) as model_client:
                return await asyncio.wait_for(loop(model_client), timeout=max(0, deadline - time.monotonic()))
        except (asyncio.TimeoutError, ModelRunTimedOut):
            events.record("model_timeout", limit_seconds=loop_timeout)
            raise ModelRunTimedOut("Model loop timed out; no further tools were executed") from None
        finally:
            persist()
