# AgentCheck-Biz

**面向 LLM Agent 与 MCP 工具的业务结果验证工作台。**

AgentCheck-Biz 基于 [AgentCheck](https://github.com/aritra741/AgentCheck) 扩展，通过故障注入、独立业务观察和修复前后对比，验证 Agent 执行后的实际业务状态。例如：创建工单已经提交，但工具响应丢失；Agent 重试后，系统是否产生了重复工单？

平台保留工具调用、事件、数据库或只读 API 观察以及逐项检查结果，帮助区分“执行结束”“工具返回成功”和“业务契约满足”。

**当前演示版本：`v2.0.0-rc.1`（V2 候选版）。** 2026-09-15 的工程验收已通过，独立第三方试用反馈仍待取得。详见 [交付记录](docs/v2/d35.md) 和 [验收结果](docs/v2/d35-results.json)。

[安装与运行](#安装与运行) · [演示流程](#演示流程) · [版本与分支](#版本与分支) · [文档导航](#文档导航)

## 核心能力

| 能力 | 实现与用途 |
| --- | --- |
| 业务结果检查 | 独立读取 Ticket 的 SQLite 数据库或 Gitea 的只读 API，验证资源数量、字段和操作归属 |
| 故障注入 | 覆盖响应丢失、短暂不可用、权限拒绝等场景；网络实验通过独立 HTTP 代理执行 |
| 修复前后对比 | 在相同案例下比较缺陷服务与幂等修复服务，保留业务失败及其证据 |
| MCP 接入 | 使用官方 MCP Python SDK，通过 stdio 连接 Ticket HTTP 与 Gitea 业务工具 |
| 中断与恢复验证 | 使用 PostgreSQL 检查点、预算和租约，验证 worker 中断后的恢复与业务确认 |
| 可视化报告 | 展示运行历史、逐项检查、事件、版本对比和恢复详情，支持下载报告 |
| 自动化验收 | 提供 Windows 核心测试与前端构建、Linux 容器网络与恢复验收，以及已知重复缺陷门禁 |

默认演示使用确定性客户端，无需模型密钥，不发送模型请求。真实模型工作台和模型实验需单独配置。

## 版本与分支

截至 2026-09-15，远程仓库有三个分支。它们保存不同阶段的代码，不需要同时运行。

| 分支 / 标签 | 当前用途 | 演示选择 |
| --- | --- | --- |
| `main` | 默认分支，仍停留在上游基线 `2b89d2c`，尚未包含 V2 候选交付 | 不用于本项目的 V2 业务演示 |
| `ci-validation` | D33 阶段的 CI 验证分支，保留自动化验收与回归红绿灯的历史证据 | 用于查阅历史验收 |
| `release-candidate` | 当前 V2 候选交付分支，包含安装、演示、恢复页面和服务生命周期入口，以及后续文档维护 | **查看最新说明与候选代码** |
| `v2.0.0-rc.1`（标签） | 固定在已验收的候选提交 `852b039`；后续文档维护不移动此标签 | **推荐用于可复现演示** |

分支用于隔离开发和保留阶段成果，后续提交会推动分支向前；标签用于标记某次发布。`rc.1` 表示第一个发布候选版，目前尚未宣称正式稳定版本。

后续维护建议：完成候选验收与试用后，将交付改动合并到 `main`，让默认分支成为对外入口；开发改动通过短期分支和 Pull Request 合入，阶段版本用标签留存。这是后续维护建议，不表示当前已完成合并。

## 安装与运行

### 1. 准备环境与源码

以下命令适用于 **Windows PowerShell**。候选版已验证的工具版本为：

| 工具 | 验证版本 / 说明 |
| --- | --- |
| Python | 3.10.11 |
| Node.js / npm | 22.19.0 / 10.9.3 |
| Git | 使用下方克隆命令时需要；也可下载源码包 |
| PostgreSQL / Gitea | 由项目脚本准备固定版本，本地演示逐场景启动 |

首次准备需要联网下载依赖和二进制；准备完成后的业务实验只访问本机服务。建议为演示预留 4 GB 内存和 4 GB 磁盘，实际需求随运行产物增加。Linux 网络与恢复验收见 [容器复现说明](docs/v2/reproduce.md)。

在一个新目录获取固定演示版本：

```powershell
git clone --branch v2.0.0-rc.1 --depth 1 https://github.com/JayusChi/AgentCheck-Biz.git AgentCheck-Biz-demo
Set-Location AgentCheck-Biz-demo
```

按标签克隆后 Git 会处于 detached HEAD（固定提交）状态，可直接安装和运行。如需修改代码，先创建自己的开发分支。也可从 [RC1 发布页](https://github.com/JayusChi/AgentCheck-Biz/releases/tag/v2.0.0-rc.1) 下载源码包并解压到新目录。后续命令均在项目根目录执行。

### 2. 安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-acceptance.txt -r requirements-persistence.txt
.\.venv\Scripts\python.exe scripts/fetch_postgres.py
.\.venv\Scripts\python.exe scripts/fetch_gitea.py
```

### 3. 生成演示结果并启动页面

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/delivery.py demo --directory artifacts/delivery
.\.venv\Scripts\python.exe -X utf8 scripts/delivery.py serve --directory artifacts/delivery --port 8035
```

`demo` 会安装前端锁定依赖并构建页面，运行核心测试、网络与恢复实验、A/B/C 对照和重复缺陷门禁，生成本次演示数据。完整过程需要数分钟，建议在讲解前完成；确认成功后再运行 `serve`。

打开 **[业务结果验证页面](http://127.0.0.1:8035/business)**。服务在前台运行，按 `Ctrl+C` 可退出。重复执行 `demo` 时使用新的目录，例如 `artifacts/delivery-2`；`serve`、`health` 和 `stop` 应使用同一个目录。

### 4. 检查与停止服务

在项目根目录打开另一个终端：

```powershell
.\.venv\Scripts\python.exe scripts/delivery.py health --directory artifacts/delivery
.\.venv\Scripts\python.exe scripts/delivery.py stop --directory artifacts/delivery
```

停止后可重新运行 `serve` 查看已有结果。安装排错、组件生命周期及源码包校验见 [完整交付说明](docs/v2/delivery.md)。

## 演示流程

核心故事是：**提交成功 → 响应丢失 → 重试 → 独立检查业务结果 → 验证幂等修复。** 幂等处理使同一业务操作被重复提交时仍只创建一张工单。

| 对照 | 服务与故障 | 预期工单数 | 业务结论 |
| --- | --- | --- | --- |
| A：正常对照 | 缺陷服务，无故障 | 1 | PASS |
| B：复现缺陷 | 缺陷服务，提交后响应丢失并重试 | 2 | FAIL |
| C：验证修复 | 幂等服务，相同故障并重试 | 1 | PASS |

1. 在 `/business` 选择 **T03 / Scripted / unsafe**，运行并查看两张工单及 FAIL 检查。
2. 保持案例与执行方式不变，选择 **fixed** 再运行，查看一张工单及 PASS 检查。
3. 对比两次运行，解释提交、故障与重试的事件顺序，并下载报告。
4. 展示 `delivery.json` 中的真实网络 A/B/C 对照，以及页面中的恢复详情。

页面 T03 使用本地函数级故障；`delivery.py demo` 的 A/B/C 对照使用真实 MCP、HTTP 代理和独立提交确认后的断连。介绍时应说明两者的执行层次。完整讲解顺序见 [五分钟演示脚本](docs/v2/demo-five-minutes.md)。

**B 的 FAIL 是预期保留的业务缺陷。** 演示验收通过表示 A/B/C 现象与证据符合预期，并不表示三个业务运行都成功。权限拒绝等案例则可能以“停止且不创建”为正确结果；PASS 始终取决于案例契约。

## 验收与运行产物

2026-09-15 的交付记录包含 224 项核心测试、前端构建、3 个网络场景、4 个恢复场景及 A/B/C 对照通过；已知重复缺陷被固定发布契约拒绝。本轮没有新增模型请求。这些是已记录的候选验收结果，当前运行结果应以新生成的报告为准。

需要独立执行自动验收时，在完成上述依赖准备后运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/verify_v2.py
```

`delivery.py demo` 已包含这一步，普通演示无需重复执行。仅运行核心测试与前端构建可添加 `--scope core`。CI 定义见 [verify-v2.yml](.github/workflows/verify-v2.yml)，历史记录见 [证据索引](docs/v2/history.md)。

| 相对 `artifacts/delivery/` 的路径 | 内容 |
| --- | --- |
| `delivery.json` | 本次演示总结果、A/B/C 观察及交付状态 |
| `ci/public/` | 可分享的 JSON、Markdown 和 JUnit 验收报告 |
| `controls/` | 正常、重复缺陷、修复三组证据与独立复查 |
| `negative-gate/` | 已知重复缺陷被拒绝的预期 FAIL |
| `recovery-index.json` | 本次恢复证据索引 |
| `business-api/` | 页面运行历史、业务观察和报告 |
| `server.json` | 本次平台实例的生命周期记录 |

业务命令及验收器的状态为 `PASS=0`、`FAIL=1`、`INCONCLUSIVE=2`、`ERROR=3`。证据不足与执行错误均不能算作通过。数据库、私有日志和运行时配置可能含测试凭据，仅保存在本机；发布范围见 [交付说明](docs/v2/delivery.md)。

## 开发与模型配置

前后端分别开发时，使用已安装依赖的环境，在项目根目录启动后端：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m uvicorn dashboard.api.main:app --reload --port 8000
```

另开终端启动前端：

```powershell
Set-Location dashboard/frontend
npm.cmd ci --no-audit --no-fund
npm.cmd run dev
```

访问 [开发页面](http://localhost:5173/business)。Vite 将 `/api` 转发到后端的 8000 端口；完整恢复演示使用前述 `delivery.py serve` 入口。

如需使用真实模型工作台，将 [.env.example](.env.example) 复制为 `.env`，按所选模型服务商填写密钥与端点后重启后端。百炼使用 `DASHSCOPE_API_KEY` / `DASHSCOPE_BASE_URL`；其他服务商和辅助评分配置见示例文件。密钥仅存放在后端环境中，不提交到 Git。

真实模型运行会发送请求并可能产生费用。模型实验的计划、预算和证据边界见 [D34 请求预览](docs/v2/d34-preview.md) 与 [D34 实验记录](docs/v2/d34.md)。上游实验脚本保留在 `experiments/`，其结果与本项目业务验收分别解读。

## 项目结构

```text
agentcheck/           上游 Agent 执行、故障注入与评分引擎
agentcheck_biz/       业务契约、独立观察、网络故障、恢复及交付逻辑
dashboard/           FastAPI 后端与 React / TypeScript 前端
examples/            Ticket、Gitea、MCP 等业务接入与演示
cases/               业务案例与运行配置
schema/              案例、规则及验收清单的 JSON Schema
scripts/             依赖准备、验收、打包与演示入口
tests/               业务及 V2 自动化测试
ci/                  固定验收契约、源码白名单与容器配置
docs/                安装、设计、演示和阶段验收记录
experiments/         上游研究实验及业务实验相关材料
artifacts/           本机生成的运行结果，不随公共源码发布
```

## 适用范围与限制

- 当前交付为本地可演示的候选版，独立第三方试用尚未完成，不声明生产可靠性。
- Scripted / LangGraph 确定性客户端用于验证执行机制和业务契约，不代表模型智能评测。
- Gitea 通过独立只读 API 观察；缺少充分写入证据时保留待核实状态，不推断内部数据库提交，也不保证端到端 exactly-once（恰好一次）。
- D34 历史模型实验为 6 个槽位、16 次请求。故障样本通过查询恢复，未触发创建重试，不能据此得出幂等修复提高模型成功率的结论。
- 上游的执行器、模型诊断和研究结果与本地业务扩展分别归属，详见 [来源与贡献边界](UPSTREAM.md)。

## 文档导航

| 文档 | 用途 |
| --- | --- |
| [候选版交付说明](docs/v2/delivery.md) | 完整安装、演示、健康检查、停止与排错 |
| [五分钟演示](docs/v2/demo-five-minutes.md) | 展示顺序与证据讲解 |
| [候选交付记录](docs/v2/d35.md) / [结果索引](docs/v2/d35-results.json) | 本轮验收范围和已知保留项 |
| [独立试用说明](docs/v2/trial.md) | 试用步骤与反馈模板 |
| [自动化与容器复现](docs/v2/reproduce.md) | Windows / Linux 验收与 CI 输出 |
| [业务 MCP 接入](docs/v2/mcp-integration.md) | Ticket 与 Gitea 的协议接线与兼容范围 |
| [历史证据索引](docs/v2/history.md) | D33 CI 与 D34 模型实验 |
| [V1 本地快速开始](docs/quickstart.md) | 早期函数级工单演示；范围以该阶段为准 |
| [来源与贡献边界](UPSTREAM.md) | 上游归属及业务扩展记录 |

逐日开发和历史实验保留在 `docs/`，其中的测试数量与能力描述对应记录当时的版本；部分原始产物只保存在项目所有者本机。

## 贡献

提交问题时请注明版本或提交号、操作系统、复现步骤、预期与实际结果，并附脱敏后的公开报告。代码改动请说明影响范围和验证结果；新增发布源码时同步检查 `ci/source-files.json` 白名单。

## 许可证与致谢

本项目基于 [aritra741/AgentCheck](https://github.com/aritra741/AgentCheck)，保留上游 Git 历史与版权声明。上游研究项目为 *AgentCheck: A Reproduce–Intervene–Mitigate Workbench for LLM Agents over MCP*。

采用 [MIT License](LICENSE)。上游与本地贡献说明见 [UPSTREAM.md](UPSTREAM.md)。
