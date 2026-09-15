# AgentCheck

Code and artifacts for **AgentCheck: A Reproduce–Intervene–Mitigate Workbench for LLM Agents over MCP**.

AgentCheck connects to an MCP server (or uses bundled examples), runs a clean agent execution, replays the same run while injecting exactly one tool-response fault, scores how the agent handled the fault, and optionally re-runs with mitigations to confirm whether a fix closes the failure.

## V2 候选版交付（2026-09-15）

**工程验收通过，独立第三方试用待反馈。** 从 [候选交付说明](docs/v2/delivery.md) 开始：新环境安装 → `scripts/delivery.py demo` → `serve` → `health` / `stop`。本轮 224 项核心测试、前端构建、3 个网络和4 个恢复场景通过；真实 A/B/C 的工单数为 1/2/1，已知重复缺陷被发布门禁拒绝。均无新增模型费用。

[D35 结果](docs/v2/d35.md) · [结果索引](docs/v2/d35-results.json) · [五分钟演示](docs/v2/demo-five-minutes.md) · [试用说明](docs/v2/trial.md) · [候选发布](https://github.com/JayusChi/AgentCheck-Biz/releases/tag/v2.0.0-rc.1)

下方保留逐阶段历史记录；历史报告的本机 artifacts 链接不包含在公开源码包中。本候选包的安装和验收以以上入口为准。

## 本地业务扩展：两周验收入口

**首次使用请看 [AgentCheck-Biz 快速开始](docs/quickstart.md)**：无需密钥即可复现“提交成功但响应丢失 → 重试产生业务重复 → 幂等修复”，并在 `/business` 查看数据库、事件、检查和版本对比。这份快速开始演示 V1 本地工具；D20 已新增两对象的真实业务 MCP 接入，见下方第二版入口。

**第二版进度**：D16–D33 已完成。D33 无模型 CI 已取得真实远程绿灯、重复写入回归红灯及恢复后的绿灯；Windows 202 项测试和前端构建通过，Linux 固定容器的 3 个网络实验、4 个恢复场景通过；独立源码包本机复现通过，零付费模型请求。[一键复现说明](docs/v2/reproduce.md)；运行 `python scripts/verify_v2.py`。D34 已完成本轮获授权真实模型实验与只读证据复查：6 PASS，16 次请求，11,038 已知 tokens；未触发创建重试的样本不作去重有效性结论。见 [D34 实施记录](docs/v2/d34.md)与[完整请求预览](docs/v2/d34-preview.md)。

D13 新增固定预算实验入口 `python -m agentcheck_biz.experiment`，默认只预览；`--live` 才发送付费请求。固定 F1/F2/F3 × A/B/C，共 9 次运行，最多 45 次请求，逐样本保留轨迹和所有结论。D14 的自动验收入口仍为下方 `verify_clean.py`；项目所有者已完成真实试用，滚动与旧接口 422 问题处理后确认测试无问题、T03/T07 可理解，见 [试用反馈](docs/day14-user-feedback.md)。最新证据见 [D13 / D14 记录](docs/day13-day14-walkthrough.md)，贡献归属见 [UPSTREAM.md](UPSTREAM.md)。

D10 历史验收已在同机 Windows 的新源码副本、新 Python 虚拟环境和新前端安装中通过：当时 75 项自动化测试、核心案例 4 PASS、完整修复版 12 PASS；缺陷版保留 4 FAIL。前端重建与真实 HTTP 运行 / 报告下载通过，未复制 `.env` 或运行数据，未发送新模型请求。最新测试数量见 D13 / D14 记录。

```powershell
# Windows + Python 3.10 + Node.js/npm；需联网下载锁定依赖
python scripts/verify_clean.py
```

脚本为每次验收建立独立目录并打印 `acceptance.json` 路径。手动安装、五分钟演示和适用边界见 [D10 跟读说明](docs/day10-walkthrough.md)、[验收记录](docs/day10-results.json) 与 [第二周复盘](docs/week2-review.md)。下方上游工作台安装方式及模型配置仍保留。

## Layout

- `agentcheck/` — comparison engine, fault injection, deterministic pass/fail scoring, LLM-judge diagnostics
- `dashboard/` — FastAPI backend + React frontend
- `agent_specs/` — bundled example specs
- `templates/` — 120-scenario suite
- `experiments/` — experiment runners + annotation UI
- `results/` — experiment outputs
- `dashboard/seed/agentcheck.db` — precomputed comparisons for **Explore examples**

## Setup

```bash
cd AgentCheck
python -m venv .venv
source .venv/bin/activate
pip install -e .
pip install -r dashboard/requirements.txt
cp .env.example .env
# Edit .env — see that file for workbench keys and multi-model experiment keys
```

Frontend (development mode):

```bash
cd dashboard/frontend
npm install
```

## Running the workbench

### Development

Terminal 1 — backend:

```bash
source .venv/bin/activate
PYTHONPATH=. uvicorn dashboard.api.main:app --reload --port 8000
```

Terminal 2 — frontend:

```bash
cd dashboard/frontend
npm run dev
```

Open [http://localhost:5173](http://localhost:5173).

### Single-server

```bash
source .venv/bin/activate
PYTHONPATH=. uvicorn dashboard.api.main:app --port 8000
```

Open [http://localhost:8000](http://localhost:8000). **Explore examples** works from the seed database without API keys.

## Experiments and results

| Results dir | Study |
|-------------|-------|
| `results/injection_validation/` | Injection validation |
| `results/fixed_response_repeatability/` | Fixed-response repeatability |
| `results/judge_repeatability/` | Judge repeatability |
| `results/comparative_profiling/` | Comparative agent profiling |
| `results/mitigation_impact/` | Mitigation impact |

The suite is **120** scenarios (10 per fault type). Experiment runners use `evaluate.py` (MCP comparison → deterministic fault-handling checks → optional LLM judge). Summaries live in each results directory as `summary.json`.

### Annotation UI

Self-contained HTML annotator (no server):

```bash
open experiments/annotation_ui.html
```

Regenerate from injection-validation traces:

```bash
python experiments/export_annotation_ui.py --html-only
```

### Re-run experiments

Requires API keys (see `.env.example`). Comparative profiling and mitigation impact need keys for every agent you run:

```bash
python experiments/run_injection_validation.py
python experiments/run_fixed_response_repeatability.py
python experiments/run_judge_repeatability.py
python experiments/run_comparative_profiling.py
python experiments/run_mitigation_impact.py
```

Default judge model is `claude-haiku-4-5-20251001`. Use `--no-judge` for deterministic pass/fail without diagnostic labels.

## Environment variables

### 阿里云百炼（本地工作台）

在根目录 `.env` 中设置 `DASHSCOPE_API_KEY` 和 `DASHSCOPE_BASE_URL`。
地址使用百炼控制台导出的 `openAiCompatible` 值，须与密钥所在地域、业务空间匹配。
密钥只放在后端 `.env`，不要写入前端变量或提交到 Git。

如需同时使用百炼做辅助评分，增加：

```dotenv
AGENTCHECK_JUDGE_MODEL=qwen3.7-max
AGENTCHECK_JUDGE_PROVIDER=bailian
```

重启后端，刷新前端，在“连接 MCP 服务器”中选择“通义千问 3.7 Max（阿里云百炼）”。
首次可选“内置演示 MCP”及“原生工具调用”，保持任务和 A1 超时配置，点击“运行对比”。
被测模型与辅助评分模型均为 Qwen 时，评分结果不能视为独立模型的复核。
若返回 `Access denied by API-Key restrictions`，检查百炼控制台该密钥的模型访问范围、IP 白名单等限制。

原有服务商配置如下：

Copy `.env.example` to `.env` and fill in keys. Live workbench runs need `OPENAI_API_KEY` (agent) and `ANTHROPIC_API_KEY` (Claude judge). Multi-agent experiment re-runs also need provider keys for Gemini, DeepSeek, and Llama — see `.env.example`.

## 本地业务结果验证扩展（D3 / D4）

基于 AgentCheck 新增的工单验证示例：在事务提交后模拟工具响应丢失，独立读取 SQLite 判定业务结果，再用服务端幂等版本复测。当前通过源码根目录运行，使用受控客户端，不调用模型；尚未接入上游 MCP 执行器；D9 已新增独立业务报告页面，见下文。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m examples.ticket_agent.d3_demo
.\.venv\Scripts\python.exe -X utf8 -m examples.ticket_agent.d4_demo
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests/business -p 'test_*.py' -v
```

D4 预期对照：正常原版本 PASS（本次操作 1 张工单）；故障原版本 FAIL（2 张）；相同故障下的幂等版本 PASS（1 张）。每阶段独立建库，保存 JSON 检查、事件、真实数据库及可阅读报告；路径由命令打印。无关初始工单另行检查，不计入本次操作数量。

单独运行 `d4_demo --phase B` 返回业务失败退出码 1，`--phase C` 返回通过退出码 0；完整 A/B/C 命令在预期对照全部复现时返回 0，仍保留 B 的业务 FAIL。

阅读 [D3 跟读说明](docs/day3-walkthrough.md)、[D4 跟读说明](docs/day4-walkthrough.md) 和 [来源与贡献边界](UPSTREAM.md)。

## D5 真实模型业务入口（已实测）

已增加百炼原生工具调用适配器，复用 D4 工单服务、故障和独立检查。模型可以创建或查询当前业务请求的工单；本地上下文、工具调用预算和模型请求上限由程序控制。

D5 验收时 40 项离线检查通过。2026-09-08 获得明确联网授权后，`qwen3.7-max` 正常及 F1 两组真实业务运行均 PASS，共 5 次模型请求、已知 3297 token。故障组在结果未知后主动查询已提交工单，没有再次创建。首轮连接 ERROR 也保留在完整记录中。见 [D5 实测结果](docs/day5-results.md)。

使用已配置并获授权的模型服务时，可在源码根目录运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m examples.ticket_agent.d5_demo --phase clean
.\.venv\Scripts\python.exe -X utf8 -m examples.ticket_agent.d5_demo --phase faulted
```

上述命令使用现有百炼凭据，会发送真实模型请求。详细参数、发送数据及当前状态见 [D5 跟读说明](docs/day5-walkthrough.md)、[请求预览](docs/d5-request-preview.json) 和 [第一周复盘](docs/week1-review.md)。

## D6 / D7 案例 CLI 与三类故障（已完成）

D6 / D7 验收时 62 项本地测试通过。新增严格案例校验、单例 / 套件命令、运行中状态持久化、只读复查，以及提交前短暂不可用 F2 和持续权限拒绝 F3。F2 有限重试后创建一张工单；F3 拒绝后停止且数据库不变，两者采用不同业务通过标准。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m agentcheck_biz.cli validate --cases cases/tickets/core
.\.venv\Scripts\python.exe -X utf8 -m agentcheck_biz.cli suite --cases cases/tickets/core --agent scripted --app-version fixed
```

幂等版本的正常 / F1 / F2 / F3 四个核心案例均 PASS；缺陷版本在 F1 下产生重复并使套件返回 FAIL。所有结论保留在 `suite.json`，可从同目录 `suite.md` 查看各运行。退出码为 PASS=0、FAIL=1、INCONCLUSIVE=2、ERROR=3，错误配置在执行前被拒绝。

本次未发送新的模型请求。完整命令、案例规则、运行状态、实测证据和学习顺序见 [D6 / D7 跟读说明](docs/day6-day7-walkthrough.md)。该目录保留 4 个核心案例；D8 的完整 12 案例套件见下文。

## D8 / D9 完整案例与业务报告页面（已完成）

完整套件位于 `cases/tickets/full/`：修复版 **12 PASS**，缺陷版 **8 PASS / 4 FAIL**，失败覆盖请求重放、丢响应重试、内容冲突和并发创建。75 项自动化测试与前端构建通过。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m agentcheck_biz.cli suite --cases cases/tickets/full --app-version fixed
.\.venv\Scripts\python.exe -X utf8 -m uvicorn dashboard.api.main:app --host 127.0.0.1 --port 8019
```

打开 [本地业务报告页面](http://127.0.0.1:8019/business)，选择案例和版本后运行。页面显示执行阶段、业务结论、逐项期望 / 实际、事件及原始观察，可下载报告。API 使用单个受控子进程，45 秒硬超时，忙时拒绝重复提交；按单个 Uvicorn worker 运行。

这一入口仅运行确定性案例，不调用模型。完整案例表、检查器负例、API 说明、实测报告与学习步骤见 [D8 / D9 跟读说明](docs/day8-day9-walkthrough.md)。

## D11 / D12 运行对比与 LangGraph（已完成）

业务页面新增同案例左右对比，列出变化 / 一致配置及每项业务结果；多项变化、未命中故障或证据不足时阻止单项归因。新增实际 LangGraph 状态图执行方式，复用原工具预算和独立检查器，并保存真实节点 / 路由事件。

88 项测试与前端构建通过。LangGraph 支持 T01–T08、T11：修复版 9 PASS；缺陷版 6 PASS / 3 FAIL。T09、T10、T12 继续使用 Scripted；不支持的组合会被拒绝。本次没有调用付费模型。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m agentcheck_biz.cli suite --cases cases/tickets/core --agent langgraph --app-version fixed
.\.venv\Scripts\python.exe -X utf8 -m uvicorn dashboard.api.main:app --host 127.0.0.1 --port 8021
```

打开 [业务对比页面](http://127.0.0.1:8021/business)。按 [D11 / D12 学习说明](docs/day11-day12-walkthrough.md) 先固定执行方式比较服务版本，再固定服务版本比较适配器。兼容范围、CLI / API、状态图和原始证据见说明及 [验收索引](docs/day11-day12-results.json)。

## License

MIT — see [LICENSE](LICENSE).
