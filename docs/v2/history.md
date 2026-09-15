# 历史证据索引

以下属于已完成的历史实验，不与本轮候选验收或新模型请求数混合。

## D33 远程 CI（2026-09-14）

公开仓库 `JayusChi/AgentCheck-Biz` 的 `codex/d33-ci-20260914-62fc62cb` 分支：

| 对照 | 提交 | 实际运行 |
|---|---|---|
| 正常候选 | c6c04eb2c8e3 | [绿灯 34823541779](https://github.com/JayusChi/AgentCheck-Biz/actions/runs/34823541779) |
| 已知重复缺陷 | 95e139afec92 | [红灯 34823817145](https://github.com/JayusChi/AgentCheck-Biz/actions/runs/34823817145) |
| 恢复正常候选 | ff258630b6a3 | [再次绿灯 34824273027](https://github.com/JayusChi/AgentCheck-Biz/actions/runs/34824273027) |

Windows 当时为 202 项核心测试与前端构建；Linux 容器为 3 个网络与4 个恢复场景。缺陷只把 Ticket 候选改为 unsafe，实际产生2张工单，仍面对原先的单工单契约。没有把配置报错或未执行冒充业务回归红灯。

## D34 真实模型实验（2026-09-14）

6 个固定槽位，16 次真实请求/响应，11,038 已知 tokens，未知用量请求0；4/4故障命中、2次崩溃恢复，独立业务观察6/6。费用未取得供应商账单，不能记为0。

计划 SHA-256：`8c304f884a091999b85b4248c3523bb2f1dd31e0f71cd0effc776d5c6caedb1e`。

原始凭据、授权附件、运行数据库和完整轨迹保留在所有者本机，未放进公共源码包。D35 再次只读复查了6个槽位，结果记入 [本轮结果索引](d35-results.json)。故障样本都通过查询恢复，没有创建重试；不能由6 PASS得出幂等修复改善模型表现或普遍成功率。
