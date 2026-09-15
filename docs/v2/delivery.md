# V2 候选版：安装、演示与停止

本项目基于 AgentCheck 扩展。D35 提供可复现的本地候选版；独立第三方试用反馈另行记录，不能由自动化验收替代。最新状态见 [交付记录](d35.md)。

## 从源码包开始（Windows）

使用 Python 3.10.11、Node.js 22.19.0 / npm 10.9.3。解压到较短的新目录，例如 `D:\AgentCheck-RC`，在该目录的 PowerShell 运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-acceptance.txt -r requirements-persistence.txt
.\.venv\Scripts\python.exe scripts/fetch_postgres.py
.\.venv\Scripts\python.exe scripts/fetch_gitea.py
.\.venv\Scripts\python.exe -X utf8 scripts/delivery.py demo --directory artifacts/delivery
.\.venv\Scripts\python.exe -X utf8 scripts/delivery.py serve --directory artifacts/delivery --port 8035
```

访问 <http://127.0.0.1:8035/business>。`demo` 会安装锁定的前端依赖并构建，运行核心测试、网络和恢复实验、A/B/C 对照及已知重复缺陷门禁，然后生成本次恢复页面索引。服务在前台运行，可按 Ctrl+C 退出。重复 demo 必须使用新的目录，如 `artifacts/delivery-2`；保留失败证据。

首次安装需网络，运行实验只使用本机 loopback，模型请求数为 0。Linux 的网络与恢复验收走 [容器 CI 入口](reproduce.md)，本页交互启动器的验证平台是 Windows。

## 健康检查和停止

在同一源码目录打开另一个终端：

```powershell
.\.venv\Scripts\python.exe scripts/delivery.py health --directory artifacts/delivery
.\.venv\Scripts\python.exe scripts/delivery.py stop --directory artifacts/delivery
```

健康检查同时核对端口、进程和本次实例标识。停止命令让原控制器自行退出；不会根据磁盘记录里的 PID 杀死其他程序。停止平台后可以用同一条 serve 命令重新打开历史。所有业务运行和恢复页面读取本次 `artifacts/delivery` 目录，不依赖项目所有者的 D30 数据。

## 各组件怎样启动

| 组件 | 生命周期与证据 |
|---|---|
| 平台 FastAPI / React | serve 持续提供页面；health 核对当前实例；stop 或 Ctrl+C 停止 |
| Ticket HTTP 与 SQLite | 每个网络/恢复案例创建独立服务与库；结束后保留只读观察和清理记录 |
| MCP stdio | 由场景启动官方 SDK 的客户端与工具子进程；记录初始化、工具发现与调用 |
| HTTP 故障代理 | 每例独立规则与端口；提交后独立确认再断开响应；结束后关闭 |
| Gitea 1.26.4 | 使用校验后的官方二进制、临时私有仓库和专用账户；逐例隔离并关闭 |
| PostgreSQL 17.11 | 创建本次状态库；实际检查点、预算和租约；结束后停止本次集群 |
| Agent worker | 在指定窗口终止和恢复；保存新旧 worker、检查点和业务确认记录 |

上述工具组件按场景按需启动，不作为一组长期空闲服务保留。验收控制器持有进程句柄，Windows Job Object 负责异常退出时收回子进程。临时账户只用于本地测试。

## 输出与资源

- `artifacts/delivery/delivery.json`：本次演示结果、A/B/C 对照和剩余交付状态。
- `ci/public/`：可分享的 JSON、Markdown、JUnit 三份受限报告。
- `controls/`：实际正常、重复缺陷和修复证据；`negative-gate/` 保留预期 FAIL。
- `recovery-index.json`：绑定本次恢复套件文件摘要的索引。
- `business-api/`：页面运行、数据库观察、报告及历史。
- `server.json`：平台生命周期记录。报告中的业务 FAIL 与实验验收 PASS 分开解释。

建议预留 4 GB 内存和 4 GB 磁盘作为本地演示起点；这是使用建议，不是实测最低配置。首次安装及完整自动化演示需数分钟，实际时长见新环境 `fresh.json`。PG 配置为 32 MB shared_buffers、最多 20 个连接。端口冲突时选择其他端口，无需停止旧服务。

`private/`、数据库、运行时配置与 Gitea 目录可能含测试凭据，只留本机。公开发布源码白名单和 `ci/public/`，不要直接打包整个工作目录。

## 源码校验与再复现

```powershell
python scripts/delivery.py verify-bundle --archive agentcheck-v2-source.zip
python scripts/verify_delivery.py --directory artifacts/fresh-check
```

后者新建源码副本和虚拟环境，安装冻结依赖、准备二进制，并完成整套 D35 演示。`--binary-cache` 可复制当前目录内已经通过 SHA-256 核对的官方 PG 安装归档和 Gitea 程序；仍会新建状态库、虚拟环境、前端安装和所有业务数据，不复用历史服务。

## 常见问题

- 安装失败：保留输出目录与日志，检查网络，下一次选择新目录。
- 恢复详情 503：证据缺失或文件摘要发生变化；重新执行 demo 生成新证据，不修改结论。
- 历史为空：新目录正常从空历史开始，在页面运行 T03 即可。
- Gitea/PG 启动失败：查看对应私有日志，确认固定二进制已下载；不连接个人已有业务服务。
- 崩溃导致 server.json 停留 running：先用 health 判断，使用新的 demo 目录恢复演示，避免误关其他进程。
