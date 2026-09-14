# D33 无模型 CI 与独立复现

当前验收状态见 `d33.md`。此入口执行 D33 固定范围，不重复依赖旧产物的 D16–D32 历史全量验收。没有 `--live` 参数，不需要 `.env` 或模型密钥。

## Windows

需要 Python 3.10.11、Node 22.19.0（含 npm）、可下载依赖的网络。解压源码到新的目录，在该目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-acceptance.txt -r requirements-persistence.txt
.\.venv\Scripts\python.exe scripts/fetch_postgres.py
.\.venv\Scripts\python.exe scripts/fetch_gitea.py
.\.venv\Scripts\python.exe -X utf8 scripts/verify_v2.py
```

最后一条是一键验收：配置校验、依赖检查、确定性测试、`npm ci` 和前端构建、Ticket/Gitea 网络集成、PostgreSQL 恢复集成、独立只读重判、JSON/Markdown/JUnit 导出、白名单源码包。每次创建全新产物目录；已有 `--output` 目录会拒绝。管理员环境自动使用仓库内的降权启动器，Windows Job Object 在结束时关闭本次进程树。只监听 127.0.0.1，不连接旧业务服务。

`--scope core` 与远程 Windows job 对齐；`--scope integration` 只运行集成。普通本机默认 `all`。原始日志、临时凭据和数据库只留在本机 `private` 或运行时目录，不能作为 CI 上传目录。

## Linux 容器

Python 基础镜像固定为 3.10.20-bookworm 的 SHA256；与 Windows 的 3.10.11 分别记录。Python 库、前端锁文件、Gitea 1.26.4 与 PostgreSQL 17.11 的平台二进制校验值固定。运行时不安装依赖，不访问公网；下载只在准备和构建阶段。

```bash
python3 scripts/export_v2_source.py --output source.zip
python3 -m zipfile -e source.zip extracted
docker build --platform linux/amd64 -f extracted/ci/Dockerfile.v2 -t agentcheck-v2-d33 extracted
container=$(docker create --platform linux/amd64 --init --network none --cpus 2 --memory 4g --cap-drop ALL --security-opt no-new-privileges agentcheck-v2-d33)
trap 'docker rm -f "$container" >/dev/null' EXIT
docker start "$container" >/dev/null
code=$(docker wait "$container")
mkdir -p linux-public
docker cp "$container:/workspace/artifacts/ci/linux/public/." linux-public/
exit "$code"
```

容器使用非 root 用户、独立 PID/网络空间、无主机端口映射和数据卷。只清理本次容器 ID。Linux 集成入口直接复用现有网络与恢复验证器；D31 Windows manifest 批处理入口不因此声明支持 Linux。宿主 runner 镜像由 GitHub 更新，不宣称固定整个操作系统。

## CI 门禁与输出

`.github/workflows/verify-v2.yml` 有独立的 Windows core/frontend 和 Linux container/network/recovery 两个 job。Actions 固定提交号，token 仅有 `contents: read`，checkout 不保留凭据。没有模型 secrets、pull_request_target 或付费模型步骤。

`ci/v2-contract.json` 默认选择 `C_fixed_loss_retry`。受控红灯验收将候选改成 `B_unsafe_loss_retry`：真实服务在丢失响应后重试，生成两个 Ticket；独立验证器仍要求修复版本的“只有一个 Ticket”契约，因此返回 FAIL/1。Gitea 丢失响应及 unknown-empty 恢复保留原始 ERROR/INCONCLUSIVE 业务语义；其故障实验验收通过不等于业务成功。

总进程退出码：PASS=0、FAIL=1、INCONCLUSIVE=2、ERROR=3。未执行步骤仍占位。JUnit skipped 不会令进程返回零。下载或基础环境失败也会使 CI 失败，不能记为业务回归红灯。

只上传 `public/summary.json`、`public/report.md`、`public/junit.xml`，保留 7 天。采用字段投影，丢弃任意异常字符串、HTTP body/header、令牌、路径和原始轨迹；保留阶段状态、测试计数、源码摘要和业务数量。构建日志由 Actions 自身保留，但不传入个人凭据。白名单源码包由 `ci/source-files.json` 决定，拒绝链接、越界路径、`.env`、缓存、数据库、历史产物与私钥形状内容；包内 `SOURCE-SHA256.json` 可以逐文件校验。添加源码时必须审阅并更新白名单。

离线运行需要预先准备依赖和二进制；源码包不包含个人 `.venv`、Node 模块或外部服务数据。模型调用统计为此确定性验收范围的 0，不代表验证真实模型效果。
