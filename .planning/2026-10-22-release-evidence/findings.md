# 发现

- deploy_candidate 用外部 gate_passed=True 覆盖 publish_suggestion；评测报告未绑定实际发布调用。
- approve 只看状态，未核对报告；候选的风险/范围/基线/兼容要求可在评测后修改。
- deploy_candidate 没有直接测试，IMPLEMENTATION.md 和 test_verify_claims.py 记录该历史缺口。
- 发布层能验证签名/资产，但进化入口仍构造元数据清单；本轮不声称真实候选变更已执行或装载。
- eval_reports 已有持久化表，增加按 ID 查询即可，不增加表或迁移；candidate JSON 支持增量字段。
- ALN-10 已 review，新增 ALN-10-R 子卡跟踪；保留 ALN-05/07/09 原依赖与整包 owner。

- 增量实现：候选/完整报告/门禁上下文摘要，批准消费同一份报告，发布前复核实时影响范围、基线与回滚制品；进程内同一 RLock 串行三个入口。
- 显式边界：当前进化发布仍构造元数据清单；独立身份授权和跨进程发布仲裁未实现，不能用摘要或局部测试代替。
- 初轮联调漏删 save_evaluation 的旧 candidate_id 位置参数，导致9项失败；已删冗余参数，后续联合114 passed/1 skipped。
