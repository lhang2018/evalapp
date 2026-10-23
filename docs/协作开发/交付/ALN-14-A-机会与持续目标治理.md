# ALN-14-A：机会排序、持续推进、停止与递归治理

作者codex-runtime；待独立复核，不关闭ALN-14整包，不发布基线。功能源码948b433；后续防御修复6f451c9，最终源码6837129。实现规则见[治理契约v1](../设计/ALN-14-机会与持续目标治理契约.md)。

## 已交付

- OpportunityService从已登记Goal或真实终态Task发现候选，按来源幂等持久保存。候选注册只创建proposed Goal，仍需独立授权。
- 排序比较预期收益、证据置信、预计USD成本、风险、时效和机会成本；未知费用/证据不排名，过期排除，同分稳定排序。规则有不可变版本，非法数值或版本重写拒绝。
- GoalGovernor消费continuous授权和15-R原预留入口。advance为显式、有步数上限的真实Controller调用，支持管理API；没有另建执行器或预算权威，没有自动后台定时守护。
- Goal族在同一写事务内只允许一个未结束的advance；等待审批/挂起的Task必须恢复原Task。持久推进命令绑定步数和结果，重复命令不新增动作；中断只恢复Task引用，不把重试当成重新发送许可。
- 只把预先登记的客观验收条件首次通过计为新进展；部分条件通过可独立记录，不掩盖Task失败。非空回答、重复通过、未知费用/副作用及不确定检查不计改善。达成全部登记条件转review，不能自动completed。
- 无改善次数、失败次数、重复状态、空闲时间、尝试总数达到版本阈值后停止。真实预算失败和未知证据立即停止。停止门禁同时约束实际模型/工具、普通execute、子目标及后代，持久暂停记录关联policy/Task。
- 子Goal创建事务限制深度、直接子数、全后代数及规范化目的重复/祖先循环，取消子目标也计数，不能递归重置。跨连接竞争最后一个子Goal槽和advance领取都只有一个赢家。
- 人工复盘可开启新轮，保留已达成条件、旧观察和所有历史，不补满预算，不清空递归数量。未知费用须有真实持久reconcile记录并解决待定副作用，不能仅提交一个字符串引用解锁。
- 候选注册时冻结预期评估。outcome将候选→Goal→Task、客观条件和实际预留结算金额关联；没有对应消费回执时cost_complete为false。production_benefit_verified保持false，不把预期效用或Mock结果冒充生产收益。

## 代码与管理入口

新增goals/governance.py、goals/opportunities.py、core/schema/goal_governance.py与tests/test_goal_opportunities.py。共享接线位于GoalStore创建/授权动作门禁、RunManager结束观察、App装配及goals_api；复用原Task、Goal状态机、授权、运行租约、动作守卫和预算账户。

| 管理入口 | 作用 |
|---|---|
| POST /goal-policies | 安装不可变策略ref/rules，必须带reason |
| POST /goals/{id}/objectives | 首次执行前登记objectives，带expected_revision/reason |
| POST /goals/{id}/advance | command_id、max_steps（1..32），真实执行有界推进 |
| GET /goals/{id}/progress | 查询阈值、客观条件、任务引用与停止原因 |
| POST /goals/{id}/review | 人工复盘；expected_revision/reason/evidence_refs |
| POST /opportunities/discover | source_kind、source_id、expires_at、reason |
| POST /opportunities/{id}/assessment | 带版本号的预期收益、证据、费用、风险、机会成本评估 |
| POST /opportunities/{id}/register | 只注册未授权Goal |
| GET /opportunities | 排序候选，未知/过期状态明确列示 |
| GET /opportunities/{id}/outcome | 冻结预期与实际证据分别查询 |

所有写入口要求传输层admin，body不能指定actor冒充身份。读操作沿用既有operator/admin权限。默认manual-review/v1是保守的一次尝试后复盘；自定义策略需先安装，再由Goal授权引用。客观条件仅支持result_contains/result_matches/data_equals/tool_goal_reached，不接受custom函数或结果非空自评。

## 迁移与回滚

新增goal_policies、goal_controls、goal_observations、goal_opportunities、goal_advances五张域表及索引，自动幂等发现；不改历史迁移或SCHEMA_VERSION。没有启用治理的原普通人工Goal保持原语义；continuous/advance不能把旧manual-review占位引用当无限运行授权。已有子目标Task后也不能追补父目标条件追认进展。

观察和推进请求幂等持久化；重启不会丢失已停止原因或自动重发。回退前停止持续推进入口、清点未结束Task与预算/副作用；保留治理、观察、命令及对账历史，不能删表重置约束。回退到不执行治理的旧版本将失去新约束，须重新评估该部署范围。

## 验证

- 新增42项：规则版本/非法值、无改善/失败/循环/超时、祖先停止、递归数量/深度、跨连接竞争、真实Controller推进和预算失败、真实免费模型消费15-R、部分客观进展、未知费用真实对账、人工复盘不补额、持久命令/中断、API权限、候选排序/归因和缺回执成本。
- 与既有Goal/15-R/进程恢复联合135 passed（当时38项专项）；最终42项专项通过。
- 首轮948b433固定归档：1306 passed/1 skipped，线程异常警告升级错误，台账/声明/协作元数据均通过。[首轮证据](../../../.planning/2026-10-23-aln14/committed-948b433.json)。
- 最终固定源码`6837129d4f5ef2ccf442dd9b9d89b9426096155c`：**1317 passed / 1 skipped**，线程异常警告升级错误；声明、台账、协作元数据通过。包含同期已提交18-E，不将其新增能力计入本卡；后续在途排除。客户端同Git tree复用**166 passed/TypeScript通过**。完整commit/tree、命令、日志及SHA256见[最终证据](../../../.planning/2026-10-23-aln14/committed-evidence.json)。

固定提交从git archive导出到当前项目outputs，排除通知、编程包和工作流等其他会话在途文件。客户端未变时严格比较android-app Git tree，复用8c479b6的166项/TypeScript原证据；没有重跑真机或外部供应商。日志按UTF-8 LF保存，SHA256绑定具体commit/tree。

## 保留范围与下一步

本轮提供显式管理入口、确定性规则与真实消费链，尚无周期守护、用户前端、跨设备通知及生产规模负载证明；排序权重和objective定义是宿主配置，不能视为已标定的业务收益模型。当前进展门禁读取目标族历史任务，长历史性能仍需测量。未知历史费用缺少对应持久预留时仍拒绝人工解锁，不能替旧账迁移造证。

真实候选宿主和生产收益仍依赖09/10；APRO资源优先级与设备资源、15-R独立复核、整包及发布基线仍另验。下一张为ALN-05-I记忆身份运行时接线。
