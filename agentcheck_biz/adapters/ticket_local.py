"""TicketLocal wraps V1 business semantics behind the four lifecycle contracts."""

import platform
import sqlite3

from agentcheck_biz.cases import CaseValidationError, validate_case
from agentcheck_biz.faults import build_fault
from agentcheck_biz.observers.ticket_local import TicketStateObserver
from agentcheck_biz.scenarios import run_scenario
from agentcheck_biz.verifiers.ticket_local import TicketBusinessVerifier
from examples.ticket_agent.fixture import prepare_fixture
from examples.ticket_agent.fixed_service import FixedTicketService
from examples.ticket_agent.llm_agent import LiveTicketAgent, ModelConfig
from examples.ticket_agent.scripted_agent import run_scripted_client
from examples.ticket_agent.service import OperationContext, TicketService
from examples.ticket_agent.tool_executor import TicketToolExecutor, ToolBudgetExceeded
from .contracts import BusinessPlugin, PluginConfigurationError


class TicketLocalEnvironment:
    def __init__(self, case, app_version):
        self.case, self.app_version = case, app_version
        self.service = None

    def prepare(self, context, events):
        db_path = context.evidence_dir / "business.sqlite"
        prepare_fixture(db_path, context.run_id, self.case, self.app_version)
        service_type = FixedTicketService if self.app_version == "fixed" else TicketService
        self.service = service_type(db_path)

    def cleanup(self, context, events):
        # All SQLite connections are scoped by context managers per call. No
        # process or persistent connection is owned here; retain evidence files.
        self.service = None


class TicketLocalExecution:
    def __init__(self, environment, case, agent, model_config):
        self.environment, self.case, self.agent = environment, case, agent
        self.executor = None
        self.live_agent = LiveTicketAgent(model_config) if agent == "llm" else None

    def execute(self, context, events):
        if self.environment.service is None:
            raise RuntimeError("TicketLocal environment is not ready")
        case = self.case
        self.executor = TicketToolExecutor(self.environment.service, OperationContext(**case["context"]),
            events, build_fault(case["fault"]) if case["fault"] else None,
            max_tool_calls=case["limits"]["max_tool_calls"], run_context=context)
        try:
            if self.agent == "langgraph":
                from examples.ticket_agent.langgraph_agent import run_langgraph_client
                return run_langgraph_client(case, self.executor, events)
            if self.live_agent:
                return self.live_agent.run(case["task"], self.executor, events, context.evidence_dir)
            if case.get("scenario", "retry") in {"retry", "replay"}:
                return run_scripted_client(self.executor.create_ticket, events, case["request"],
                                           max_attempts=case["limits"].get("max_client_attempts", 2))
            return run_scenario(case, self.executor, events)
        except ToolBudgetExceeded:
            events.record("client_stopped", reason="tool_budget_exhausted")
            return {"status": "needs_verification", "ticket_id": None}

    def metadata(self):
        result = {"tool_calls": self.executor.call_count if self.executor else 0}
        if self.live_agent:
            model = self.live_agent.metadata
            result.update(model=model, model_called=model.get("mode") == "live model" and model["model_responses"] > 0,
                          model_request_attempted=model["model_request_attempts"] > 0)
        return result


def ticket_local_plugin(case: dict, options: dict) -> BusinessPlugin:
    if set(options) - {"app_version", "inject_fault", "agent", "model_config"}:
        raise PluginConfigurationError("Unknown TicketLocal options")
    app_version, agent = options.get("app_version", "fixed"), options.get("agent", "scripted")
    inject_fault = options.get("inject_fault", True)
    if app_version not in {"unsafe", "fixed"} or agent not in {"scripted", "langgraph", "llm"}:
        raise PluginConfigurationError("Unsupported TicketLocal service version or agent")
    if type(inject_fault) is not bool:
        raise PluginConfigurationError("inject_fault must be a boolean")
    config = validate_case(case)
    if agent == "llm" and config.get("scenario", "retry") != "retry":
        raise CaseValidationError("This scenario is a scripted contract, not a model evaluation")
    graph_metadata = None
    if agent == "langgraph":
        from examples.ticket_agent.langgraph_agent import validate_support, adapter_metadata
        validate_support(config)
        graph_metadata = adapter_metadata()
    if not inject_fault:
        config["fault"] = None
    limits = config["limits"]
    model_config = options.get("model_config")
    if model_config is not None and (agent != "llm" or not isinstance(model_config, ModelConfig)):
        raise PluginConfigurationError("model_config requires a ModelConfig and agent llm")
    if agent == "llm":
        settings = {"max_model_calls": limits.get("max_model_calls", 5),
                    "max_output_tokens": limits.get("max_output_tokens_per_model_call", 512),
                    "timeout_seconds": limits.get("model_loop_timeout_seconds", 90),
                    "request_timeout_seconds": limits.get("request_timeout_seconds", 30)}
        if model_config is None:
            model_config = ModelConfig(**settings)
        elif any(getattr(model_config, name) != value for name, value in settings.items()):
            raise CaseValidationError("Model settings conflict with case limits")
    metadata = {"schema_version": 1, "case_id": config["case_id"], "app_version": app_version,
                "agent": agent, "model_called": False, "database": "business.sqlite",
                "python_version": platform.python_version(), "sqlite_version": sqlite3.sqlite_version,
                "mode": ("真实模型工具调用；本地 Python 工单工具；受控工具边界故障模拟" if agent == "llm"
                         else "受控客户端；未调用模型；本地工具边界故障模拟")}
    if graph_metadata:
        metadata.update(adapter=graph_metadata, mode="LangGraph 确定性状态图；未调用模型；本地工具边界故障模拟")
    environment = TicketLocalEnvironment(config, app_version)
    return BusinessPlugin("ticket-local", 1, config, config["context"]["operation_id"],
        limits.get("model_loop_timeout_seconds", 90) if agent == "llm" else 45,
        f"biz-{agent}-{app_version}-{'faulted' if inject_fault else 'clean'}", metadata,
        environment, TicketLocalExecution(environment, config, agent, model_config),
        TicketStateObserver(), TicketBusinessVerifier())
