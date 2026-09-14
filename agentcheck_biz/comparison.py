"""Read-only paired reports with explicit controls; never infer causality from scores."""

from pathlib import Path

from .cases import validate_case
from .checks import load_json
from .verifiers.ticket_local import recheck_ticket_run as check_run


def compare_runs(left_dir: Path, right_dir: Path):
    left_dir, right_dir = Path(left_dir).resolve(), Path(right_dir).resolve()
    if left_dir == right_dir:
        raise ValueError("请选择两次不同的运行")
    sides = []
    for directory in (left_dir, right_dir):
        run = load_json(directory / "run.json")
        case = validate_case(load_json(directory / "case.json"))
        sides.append({"run": run, "case": case, "checks": check_run(directory)})
    left, right = sides
    if left["case"]["case_id"] != right["case"]["case_id"]:
        raise ValueError("只能对比同一案例的两次运行")
    dimensions = []
    for key in sorted(set(left["case"]) | set(right["case"])):
        dimensions.append(("case." + key, left["case"].get(key), right["case"].get(key)))
    for key in ("app_version", "agent", "adapter", "plugin", "transport", "http_timeouts", "http_auto_retries",
                "implementation_sha256", "python_version", "sqlite_version"):
        dimensions.append(("run." + key, left["run"].get(key), right["run"].get(key)))
    controls = [{"field": key, "left": a, "right": b, "same": a == b} for key, a, b in dimensions]
    changed = {item["field"] for item in controls if not item["same"]}
    if changed == {"run.app_version"}:
        kind = "service_version"
    elif "run.agent" in changed and changed <= {"run.agent", "run.adapter"}:
        kind = "adapter"
    elif not changed:
        kind = "repeat"
    else:
        kind = "multiple_changes"
    blockers = []
    for index, side in enumerate(sides):
        label = "左侧" if index == 0 else "右侧"
        if side["run"].get("execution_status") != "completed" or side["checks"]["status"] not in {"PASS", "FAIL"}:
            blockers.append(f"{label}运行不完整或证据不能判定：{side['checks']['status']}")
        if not all(side["run"].get(key) for key in ("implementation_sha256", "python_version", "sqlite_version", "agent")):
            blockers.append(f"{label}缺少版本或环境信息")
        if side["run"].get("app_version") not in {"unsafe", "fixed"}:
            blockers.append(f"{label}缺少有效服务版本")
        if side["run"].get("agent") not in {"scripted", "langgraph"}:
            blockers.append(f"{label}不是本比较器支持的确定性执行方式")
        if side["run"].get("agent") == "langgraph" and not side["run"].get("adapter"):
            blockers.append(f"{label}缺少 LangGraph 适配器版本信息")
    if kind == "multiple_changes":
        blockers.append("存在多项或其他配置变化，不能将结果差异归因于单项修改")
    explanation = {"service_version": "仅服务版本不同，案例、执行方式、预算和实现快照一致。",
                   "adapter": "仅执行适配器不同，服务版本与案例一致；用于验证适配器兼容性。",
                   "repeat": "已记录的输入与版本相同，这是重复运行观察。",
                   "multiple_changes": "配置变化见下表；先统一条件再判断单项修改的效果。"}[kind]
    check_maps = [{item["check_id"]: item for item in side["checks"]["checks"]} for side in sides]
    rows = [{"check_id": key, "left": check_maps[0].get(key), "right": check_maps[1].get(key)}
            for key in dict.fromkeys([*check_maps[0], *check_maps[1]])]
    summaries = []
    for side in sides:
        run, result = side["run"], side["checks"]
        summaries.append({"run_id": run["run_id"], "case_id": side["case"]["case_id"],
                          "app_version": run.get("app_version"), "agent": run.get("agent"),
                          "status": result["status"], "reason": result["reason"],
                          "client_result": run.get("client_result"), "tool_calls": run.get("tool_calls")})
    return {"schema_version": 1, "case_id": left["case"]["case_id"], "comparison_type": kind,
            "controlled": not blockers, "explanation": explanation, "blockers": blockers,
            "caveat": "这是两次具体运行的观察，不代表统计显著性；未单独运行正常对照时，不据此宣称纯故障效应。",
            "controls": controls, "checks": rows, "left": summaries[0], "right": summaries[1],
            "evidence_mode": "独立只读复查当前磁盘证据；不修改原报告"}
