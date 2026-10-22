# 发现

- 当前HEAD8692637，无.codegraph；claude-main认领ALN-00-B、ALN-06-A、ALN-08-M。
- 06/08原交付已交叉审出问题并在d77b241修复，运行时拒绝旁路在24cd524修复；本次验收不把旧问题重复算作未修复，也不自批本线补丁。
- ALN-00-B仍未正式发布；prepared_candidate24cd524已有915/1证据，客户端同tree沿用d77b241的136/typecheck。
- 其他人在途tests/test_e12_e13.py、通知与.workbuddy保留，不纳入验收提交范围。

- 干净8692637复现4 failed/1 passed：完整App三阶段计量0.6、预算0.200001、max_cost0.3任务仍SUCCEEDED；unknown3次只记1次；estimate0.5进入actual_cost；GC后同ref可登记新摘要。
- 归并为CA-08-01(P1，分类/规划计量消费未接)、CA-06-01(P2，回收身份复用)、CA-08-02(P2，估算混入实测)。不把已修路由旁路或已声明验收前置重复算作新缺陷。
- 既有相关178 passed；准备候选Git/tree/日志证据校验通过。原全量915/1为此前正常tests基线，本次显式验收脚本独立存放并保留失败，不改业务源码使其人为变绿。
