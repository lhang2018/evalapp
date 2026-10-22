# ALN-10：回归资产与独立门禁补全

2026-10-22。用户明确指定接续编号 10。首段由 opencode-evo 提供，本轮补充开发 codex-runtime。起点 HEAD 54f8058，联调期间模型线陆续提交至 4065d52；本记录区分本模块实现与其他工作包的真实端到端验收。

## 原半成品与本轮变化

| 原行为 | 本轮行为 |
|---|---|
| 只保存已转换 marker，从可变 Task 重建 | 新增 regression_assets 完整用例快照及 SHA256，包含输入/验收/约束/原版本/故障证据；源任务删除仍可恢复 |
| marker 写入异常吞掉后继续发布内存用例 | 完整快照先落盘；失败或摘要不符禁止用于门禁。旧 marker 有原始资料时一次迁入，缺资料保持 unknown |
| 同领域回归补跑草稿，其他必跑清单未消费 | 按能力、组件和精确 AssetRef 展开传递依赖，逐套消费 must_rerun_tests；未知组件保守扩大范围 |
| 主集平均值达标便可能放行 | 主集/逐领域/保留集/每套回归分别判定；原有自定义 gate 可收紧，不能覆盖独立核心拒绝 |
| 同 ID/version 数据变化仍可能复用基线 | 报告冻结用例摘要；验证基线案例集合、内容、版本、样本及明细一致性 |
| baseline 未在候选预算内，suite_ref 全局滞留 | 主 baseline、候选、holdout 和必跑回归统一预算；实际执行时逐套设置/恢复套件引用，同一控制器串行保护共享 runner |
| 评测答案会写普通记忆，失败可能回流训练 | evaluation 不读写生产记忆、不进入失败调优集；保留集禁止 training/tuning，输入重叠在执行前拒绝 |
| 样本数量足够或手填指标即可晋级 | 完整观察窗口、精确发布归因、等窗基线与已知费用才有晋级证据；长期收益也要求这些条件 |
| 报告缺最终门禁，秒级 ID 可覆盖 | UUID 评测记录保存最终 gate、逐套/逐例证据、覆盖范围、兼容依赖、费用来源及预算 |

## 接口与数据兼容

- 新表由域 schema 自动发现，CREATE TABLE IF NOT EXISTS，不修改旧表列或假冒新的 schema_version。
- EvalCase 增量字段：constraints、version_manifest、failure_fixture；旧用例缺省为空。冻结源版本缺失时原样保留未知，不能补成当前版本。
- EvalDataset.for_tuning() 与 Evaluator.run(purpose="training"/"tuning") 拒绝独立保留集。所有真实评测任务标记 source=evaluation，在运行时隔离记忆，并由既有信号系统排除生产反馈。
- component_impact 消费 ExpertRegistry.capabilities、传递依赖及现有 AssetRepository；目标可直接使用 `asset/<asset_id>@<version>`，不解析 latest/别名。报告冻结资产 ID/version/content_digest，检查依赖缺失、环、pin 与正文摘要。
- required_domains 与 min_cases_per_domain 配置共同约束覆盖；缺少必要领域样本和未知 suite 均明确拒绝。字段增量进入 impact_result 与评测报告，原依赖/候选状态机保持权威。
- Model 计量与预算沿用模型线实现。Runner 消费 unknown_model_costs、pricing_known 和费用待对账标记；费用未知保留为缺证据，不能用零费用报告放行。
- Candidate 增加 deployed_release_id，部署时记录真实发布 ID；自动监测按这个精确 ID 及 baseline_version 采集等长窗口。

## 故障夹具的真实性边界

历史故障证据被完整固化，但“输入再跑一次”不等于原网络故障、超时或权限状态已重现。默认 ControllerTaskRunner 明确标记 input_replay_only，独立 gate 据此拒绝认定故障回归通过。

隔离宿主可给 ControllerTaskRunner 注入 `fixture_runner(case, *, budget)`，返回 `(ok, latency, evidence)`；它必须在受控环境重现源故障条件、校验实际修复，并遵守传入的预算。证明包含 `fixture_reproduced=True` 和匹配原 failure_fixture 的 SHA256 `fixture_digest`。框架核验摘要、计量并保存原始逐例结果；没有该证明时门禁保持关闭。

这个接口没有修改生产工具注册表来伪造超时，也没有把 Mock 的成功冒充真实候选验证。真实候选版本应用、跨进程隔离和供应商故障复现仍由 ALN-09 的隔离宿主提供；ALN-10 本轮交付的是完整资产、消费、验证与拒绝链。

## 上线观察与收益归因

MonitorPolicy 默认要求完整 window_seconds、min_samples、发布归因、基线比较及费用证据。提前窗口可据确证安全问题回滚，但健康样本在窗口不足时只 hold。手工指标仍支持观察与回滚，不再默认触发 promote。

观测采用 `[deployed_at, deployed_at + window_seconds)` 与此前等长窗口，完成后不持续扩大当前窗口。基线版本标识不能对应真实发布时，comparison_valid 为 false，保持 insufficient_data。长期收益需要至少 20 个可归因样本及完整基线，不能从样本数直接宣布 observed。

## 验证与限制

专项：`python -m pytest tests/test_aln10_completion.py -q`，新增 **39 项**，覆盖实际重启/源任务删除、不可变/篡改/磁盘失败、默认消费、依赖闭包、必跑缺失、样本/领域/安全独立拒绝、保留集泄漏、预算、最终报告、未知费用、窗口/版本归因与故障夹具证明。

最终提交候选全量 **831 passed / 1 skipped (96.28s)**，父提交 `4065d52`，未纳入他人在途通知代码。日志见 `.planning/2026-10-22-aln10-completion/commit-candidate-tests.txt`；源码与本交付同次提交。联合专项与其他日志位于同目录。旧测试中“相同输入改案例 ID 即独立保留集”“手工成功率直接晋级”“suite_ref 结束后全局滞留”的预期已改为新的安全行为，并保留原功能验证。

全部证据为本地 SQLite、确定性 runner 和 Mock。没有真实供应商、真实候选资产修改或在线发布观察数据；这些证据缺失会实际阻断 gate/promote。原 ALN-05/07/09 依赖仍有效，不宣告整包生产验收。

回滚：停止新评测，保留新表与已有快照/报告；禁止删表后从已变更 Task 悄悄重建。若回退旧 gate，必须同时关闭自动发布/晋级，因为旧版不理解本轮新增的拒绝证据。

审阅结论：待独立审阅；ALN-10-Q 进入 review，ALN-10 整包仍待前置整合与真实验收。
