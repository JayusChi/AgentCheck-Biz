# 来源与当前贡献边界

来源：[aritra741/AgentCheck](https://github.com/aritra741/AgentCheck)。
阅读与开发基线：`2b89d2c5782ff81d20843391e1bd410d3e7ffbe9`。
项目保留上游历史与根目录 `LICENSE`；整体表述为“基于 AgentCheck 扩展业务结果验证”。

上游的执行器、故障注入、轨迹、规则检查、模型诊断、前后端和预计算案例属于上游已有能力。
开头保留 2026-09-08 完成 D3 / D4 时的新增范围；后续按日记录进展，最新为文末 D21 的独立网络故障代理。

## 本地新增

- `examples/ticket_agent/`：SQLite 工单服务、创建和查询、工具包装、受控重试客户端、幂等服务、D2 / D3 / D4 演示入口。
- `agentcheck_biz/`：事件文件、一次性响应丢失规则、读取真实业务数据库的独立检查器、具体工单运行流程及 JSON / Markdown 报告。
- `cases/tickets/D4_create.json`：正常与 F1 对照所用业务请求、初始状态及结果契约。
- `tests/business/`：真实数据库、故障、幂等并发、事务回滚和检查器反例测试。
- `docs/day2-walkthrough.md`、`docs/day3-walkthrough.md`、`docs/day4-walkthrough.md`：学习与复现说明。

## 本次对上游文件的接入改动

- `.gitignore` 增加本地 `artifacts/` 证据目录忽略规则。
- `README.md` 增加 D3 / D4 复现入口及当前范围说明。

工作区另有第一天留下的模型路由和前端改动，本文件不把它们归入 D3 / D4 的新增实现。
D3 / D4 通过独立本地模块运行，尚未接入上游 `mcp_runner.py`、缓存执行、轨迹页面或业务 MCP 服务。
不能把当前结果称为完整的 AgentCheck / MCP 集成，也不能用上游实验数字代表本地成果。

当前证据来自受控客户端和本地工具边界模拟。缺陷服务是明确保留的测试夹具，不是发现了外部产品缺陷。

## D5 进展（2026-09-08）

新增 `examples/ticket_agent/llm_agent.py`、`d5_demo.py`，复用第一天本地已有的 `pipeline/bailian.py` 连接配置；扩展工具执行器提供查询和模型调用编号关联，复用独立运行环境、检查器和报告。
新增 9 项离线模型适配测试、D5 学习说明、请求预览和第一周复盘。
用户明确授权后，D5 已取得正常与 F1 各一次真实工单业务运行，模型响应、工具调用、实际数据库及独立检查证据见 `docs/day5-results.md`。首轮连接错误仍保留。
故障组实际选择查询恢复，本次没有重试创建或触发幂等去重。业务工具仍通过本地函数执行，不把 D1 上游 MCP 示例当作本次业务 MCP 接入成果。

## D6 / D7 进展（2026-09-08）

新增 `agentcheck_biz/cases.py`、`cli.py`、`schema/business-case.schema.json`、`cases/tickets/core/` 四个核心案例及相应测试、使用说明。
扩展现有故障规则、工具执行器、客户端与检查器，覆盖提交前短暂不可用 F2、持续权限拒绝 F3，以及不同业务契约。
运行器现在持久化阶段状态，JSON 使用替换方式更新；CLI 支持校验、单例、串行套件、状态和只读复查。
62 项本地测试通过。F2/F3 尚未做新的真实模型实验，D5 历史模型记录复查仍通过。MCP / 业务 API / 前端和进程级硬终止仍不在已完成范围中。

## D8 / D9 进展（2026-09-08）

新增 `cases/tickets/full/` 12 个语义案例和 `agentcheck_biz/scenarios.py`，扩展独立检查器及反例测试。包括已有成功重放、查询恢复、持续不可用、内容冲突、租户 / 操作隔离、缺少编号和真实双线程并发；事件记录与工具调用编号支持并发写入。

新增 `dashboard/api/business.py`，通过 `dashboard/api/main.py` 接入受控本地子进程、运行状态、详情及证据下载；`BusinessRuns.tsx` / `business.css` 和 App 路由提供业务页面。原工作台保持原入口，新增 `/business`。API 的 45 秒进程硬超时不代表旧 CLI / 模型适配路径也已经统一采用进程终止。

75 项自动化测试、前端构建、浏览器 T03 PASS / FAIL 与刷新验证完成。完整套件修复版 12 PASS；缺陷版 8 PASS / 4 FAIL。详细证据见 `docs/day8-day9-walkthrough.md`。本次无新增模型请求，未把业务契约测试作为模型智能评测；尚未完成业务 MCP 接入。

## D10 进展（2026-09-08）

新增 `requirements-acceptance.txt` 固定验收依赖快照、`scripts/verify_clean.py` 独立源码副本与环境验收入口，以及 D10 结果、跟读说明和第二周复盘。脚本不复制现有 `.env`、依赖目录、数据库、seed、报告或前端构建产物；新环境中运行测试、核心 / 完整套件、`npm ci` / build 和真实 HTTP 检查。

首次受限网络安装 ERROR 保留；获准联网后的新一轮验收 PASS：75 项测试、4 个核心案例、修复版 12 PASS / 缺陷版 8 PASS 4 FAIL、5 次 HTTP 运行和报告下载。此次测试服务已退出，没有新增真实模型请求。验证范围是同机 Windows 的新环境及源码目录使用，不把它称为跨系统或 wheel 安装验收；业务 MCP 集成边界未改变。

## D11 / D12 进展（2026-09-08）

新增 `agentcheck_biz/comparison.py` 和前端 `BusinessComparison.tsx`，扩展业务 API / CLI 与原业务页面，提供同案例配对、逐项比较、控制条件检查和不能单项归因的提示。比较读取独立复查结果，不覆盖原始业务报告。

新增 `examples/ticket_agent/langgraph_agent.py`，实际使用 StateGraph 执行创建、条件路由、重试、查询与结束节点，保存框架 / 策略版本和实际图事件；复用原工单服务、工具执行器和检查器。`checks.py` 的 SHA-256 与 D10 相同。`pyproject.toml` 直接声明 LangGraph，固定依赖快照中的 1.2.11 保持不变。

88 项测试、前端构建及浏览器服务版本 / 适配器对比验证通过。图适配器支持 9 案例，修复版 9 PASS，缺陷版 6 PASS / 3 FAIL；多上下文与并发契约仍使用 Scripted。不声称新增模型智能或业务 MCP 集成。兼容说明与证据见 `docs/day11-day12-walkthrough.md` 和结果索引。

## D13 / D14 进展（2026-09-08）

新增 `agentcheck_biz/experiment.py`：固定九槽位 A/B/C 真实模型实验、默认无网络预览、逐槽位预算预留、子进程硬超时、错误停止、完整分母和逐响应部分 token 用量统计。仍复用现有模型适配器、工单服务和独立检查器，没有为了通过实验修改检查规则。

用户明确批准后实际运行 9 次，9 PASS、故障覆盖 6/6，22 次模型请求、15,143 已知 tokens。F1 两组均查询恢复，未触发去重，因此不声称修复提高了真实模型通过率。原始轨迹和全部分母见 D13 / D14 结果。

新增 7 项实验测试，当前共 95 项；扩展 `scripts/verify_clean.py` 的新手文档复制范围，新增 `scripts/package_business.py`、快速开始、试用邀请草稿及反馈模板。D14 新环境验收的实测状态以 `docs/day13-day14-results.json` 为准。

可复现源码、验证脚本与业务结果报告属于本地工程贡献。项目所有者真实试用中报告了滚动受阻与 HTTP 422，处理后确认测试无问题、T03/T07 可理解，记录见 `docs/day14-user-feedback.md`。没有代发邀请或取得独立第三方试用记录；没有新增远程 CI、业务 MCP 服务或生产环境接入。

## D16 进展（2026-09-08）

按 HTML 第二版日程完成 V1 冻结与 V2 边界设计，新增 `docs/v2/` 的基线索引、133 文件源码清单、实际依赖记录、进程/凭据边界图、决策记录及原始回归日志。源码 ZIP 与本次独立套件数据库保存在基线所指向的本地 artifacts 目录，工作区未提交状态单独记录。

本次 95 项业务测试、前端构建通过，核心 fixed 4 PASS、完整 fixed 12 PASS、完整 unsafe 8 PASS / 4 FAIL；检查器摘要与 D14 相同。没有更改业务实现、案例或检查规则，没有新增模型请求。第二对象暂选本机 Gitea Issue，接口、HTTP/MCP、代理和恢复机制尚未实现。详见 [D16 交付](docs/v2/d16.md)；D13 模型数字仍只归属于历史 V1 实验。

## D17 进展（2026-09-08）

新增通用生命周期、显式插件注册、四类协议、RunContext、版本化 Observation，以及 TicketLocal 的环境/执行/观察/验证实现；原 `run_ticket_case` 和工单 CLI 保持入口兼容。环境与客户端代码从原 runner 移入适配器，工单 `checks.py` 和全部案例/schema 保持原样。CLI、比较与实验复查增加观察证据校验，API 和报告增加观察文件下载。工具与模型循环使用共享截止时间，源码摘要覆盖新增嵌套目录。

95 项旧业务测试及新增 21 项协议测试通过；完整 fixed 12 PASS、unsafe 8 PASS / 4 FAIL。37 份 D16/D13 历史运行只读复查保持结论与文件摘要。本次无新增真实模型请求，没有实现 HTTP/MCP、Gitea 或可靠恢复；文件对象只用于自造协议测试，不算独立外部接入。详见 [D17 记录](docs/v2/d17.md)。

Markdown 计划已与 HTML 对齐至全部 27 节及 D16–D35 日程，新增 `scripts/sync_project_plan.py` 便于后续同步和检查。

## D18 进展（2026-09-08）

新增独立 FastAPI 工单服务、TicketHttp 环境和执行适配器、平台只读观察与 HTTP 证据复查。每次运行使用独立数据库、服务持有的临时 loopback 端口和内存测试身份；健康/版本端点核对真实 PID 与源码摘要，连接/读取/总超时生效，自动 HTTP 重试为 0。Windows 启动句柄对应实际解释器进程，仅回收自己创建的服务。

Local 与 HTTP 共用初始夹具和原 unsafe/fixed 事务逻辑。原工单 checks.py、案例与 schema 摘要保持不变；两处初始化失败测试只迁移 mock 位置。比较器增加插件/传输控制项，报告提供服务、观察与清理证据链接。新增 HTTPX 直接依赖和无模型验收脚本 scripts/verify_d18.py。

95 项业务 + 21 项协议 + 12 项 HTTP 测试通过。HTTP 三案例 fixed 3 PASS，unsafe 2 PASS / 1 FAIL（重放产生重复工单）；本地完整套件分布保持 fixed 12 PASS、unsafe 8 PASS / 4 FAIL。65 份 D16/D13/D17 历史运行只读复查结论和摘要不变。结果见 [D18 记录](docs/v2/d18.md)与验收索引。

本次为自建工单夹具的真实 HTTP 接入，平台和确定性客户端仍在同一进程。没有新增模型请求，没有实现独立 Gitea、业务 MCP、真实故障代理或持久化恢复。HTML 与 Markdown 计划同步标记 D18 完成、D19 待开始。

## D19 进展（2026-09-09）

新增 GiteaEnvironment、执行适配器、独立只读 API 观察器及 Issue 业务检查器，使用单独的 Gitea 案例/schema 和 D17 通用生命周期。通过官方未修改的 Gitea 1.26.4 实测私有仓库创建、Issue 创建/关闭、分页与逐资源 GET；重复按仓库及 run/operation 标记判定，同标题不视为重复。

Docker daemon 不可用，使用官方 Windows 二进制并校验下载 SHA-256。compose.v2.yaml 固定官方镜像版本与索引摘要，仅通过配置校验，没有冒充容器运行验收。Gitea 自身的源码和业务实现不属于本地贡献；本地贡献为隔离运行器、权限配置、执行/观察适配、业务判定与复现证据。API 观察不被称为内部数据库提交证明。

三个 scoped token 经环境变量进入适配器，执行只有 Issue 写权限，观察只有读取权限；新账号为专用非管理员。每个案例创建新私有仓库。Windows 原生 Git 工作目录保持短路径，与较深的证据目录分离，并限制向上查找范围。只清理自己持有的 Gitea 进程，不修改项目 Git HEAD。

147 项测试通过；Gitea 新增19项（14项真实实例集成，5项配置/协议检查）。三案例2 PASS / 1预期 FAIL；原工单与HTTP分布不变，99份历史只读复查结论及摘要不变。原工单检查器、案例与schema保持D16摘要。详见 [D19 记录](docs/v2/d19.md)及结果、版本索引。

HTML 与 Markdown 同步标记 D19 完成、D20 待开始。本次没有新增模型请求，没有实现业务 MCP、网络故障代理、持久化恢复或新增前端。

## D20 进展（2026-09-09）

使用官方 MCP Python SDK 1.30.0 的 ClientSession 与 lowlevel Server，实际协商协议2025-11-25，通过stdio将工单 HTTP 和独立 Gitea API 接到同一个 McpExecution。SDK协议实现属于官方项目；本地贡献为持有进程句柄的证据桥、业务绑定与转发、独立Oracle关联、兼容矩阵及回归验收。没有将上游比较实验数字或实现算成本次新增成果。

平台绑定 operation_id / deadline，每次调用生成新的 attempt_id，经 MCP 元数据送至后端调用证据。工具只暴露业务字段，观察/管理 token 不进入工具进程。工单继续独立只读SQLite，Issue继续独立只读API；未知工具、非法参数、只读权限不足、证据损坏不会得到PASS。通用lifecycle.py与D19摘要相同，未增加Gitea分支。

164项测试通过，其中D20新增17项；MCP两对象9次运行7 PASS / 2预期FAIL；原本地、HTTP、Gitea直接API分布不变，136份历史结论与证据摘要不变。详见 [D20记录](docs/v2/d20.md)、[工单契约报告](docs/v2/ticket-mcp-contract.md)、[Gitea契约报告](docs/v2/gitea-mcp-contract.md)和[接入指南](docs/v2/mcp-integration.md)。

本次采用Windows本机stdio及官方Gitea二进制。确定性客户端仍在平台进程；工具服务和业务目标分别独立。没有新模型请求、前端改动、真实网络故障代理、持久化恢复或容器运行验收。HTML与Markdown同步标记D20完成，下一步D21。上面的逐日范围说明为对应日期的历史状态。

## D21 进展（2026-09-09）

新增独立 HTTP/1.1 反向代理进程、版本化规则和传输证据检查。使用既有 h11 0.16.0 库解析 HTTP，库实现归属其上游；本地贡献是绑定专用目标/路由、转发前拒绝、响应延迟、完整上游响应后断开下游、次数限制、连接关联、独立观察接线和复现证据。没有用伪造HTTP错误正文冒充真实网络错误。

通过 options.proxy 包装现有两类 MCP 插件，通用生命周期保持D19/D20源码摘要。工单观察仍为SQLite只读，Gitea仍用独立只读API直连；管理/观察凭据不交给代理。规则未命中为INCONCLUSIVE，真实网络异常造成的业务ERROR与传输契约PASS分别记录。

184项测试通过（新增20项代理测试）；两对象10次运行包含2个透明对照、6个实际故障命中和2个未覆盖反例；无代理MCP、本地工单、HTTP、Gitea直接API分布不变，182份历史只读复查结论与文件摘要一致。完整回归曾检出Windows就绪文件临时读取失败，已加有期限的只读重试和回归，保留原始失败目录。详见 [D21记录](docs/v2/d21.md)和[代理契约报告](docs/v2/proxy-contract.md)。

没有新增模型请求、前端功能、容器部署、提交后屏障、断连前独立确认或恢复策略。D21证明传输阶段与之后的独立观察，不冒充D22提交后响应丢失的完整因果链。HTML与Markdown同步标记D21完成，下一步D22。

## D22 增量：提交后独立确认与真实响应丢失

2026-09-09：新增本机测试屏障与控制器、v2 确认后断连规则、工单 A/B/C 和 Gitea API 可见性证据、MCP 结果未知关联、20 项定向测试及独立验收脚本。205 项测试通过，238 份历史证据只读复查一致，新增模型请求为 0。原工单业务检查器、原案例 schema 和通用生命周期保持基线摘要。上游 runner 未因本日改造而改写；SDK、h11、Ticket 服务、Gitea 的来源边界沿用前文。详见 [D22 交付记录](docs/v2/d22.md)与[源码清单](docs/v2/d22-source-manifest.json)。
