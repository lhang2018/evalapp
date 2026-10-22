# 核对发现

- HEAD 54f8058；无 .codegraph。ALN-10 首段 a957fe8 用通用 evidence 行保存转换标记，再从可变源 Task/诊断重建，并非完整不可变回归用例。
- 现有未提交 controller.py 补丁按 domain 加跑失败回归，但尚需核对默认启动恢复、影响范围、安全集及 must_rerun_tests。
- 同时存在他人 routing_eval.py/tests、Android 通知及 ALN-09 相关在途修改，不能覆盖。
- 初次读取过长导致截断，后续限定文件区段和输出量；PowerShell 下 rg 的目录 glob 改为 rg -g 过滤。

- 已实现 regression_assets 完整不可变快照（输入/验收/约束/原版本/故障证据），保留旧 marker 审计；读取校验 SHA256，持久化失败不发布内存用例。
- 必跑 suites 逐套执行并独立门禁；按真实 case 明细与原版本摘要比较，安全失败不能被平均分抵消。新 API 提供 for_tuning/purpose 拦截保留集，evaluation 来源不会回流失败调优资产。
- 主基线/候选/holdout/影响范围回归共用实验预算，suite_ref 逐套绑定与清理，同一控制器串行保护共享 runner。评测结果落 final gate/逐例证据，UUID 防覆盖。
- 发布后按精确 release_id 和等长完成窗口采样；无基线/无归因/手工单次指标保持待观测，长期收益不靠样本数伪装因果结论。
- 其他开发者已新增 ALN-08 三次提交，最新 HEAD 4065d52；保留其模型/预算/路由代码，消费 unknown_model_costs 与执行租约上下文。

- 后续核对确认评测流量会经 _remember 写入生产记忆。已对 evaluation 禁用普通记忆读写，并实测正常任务仍能检索已有记忆。既有 signals 已排除评测流量，未重复改动。
- 故障输入重跑不能证明故障条件已重现。新增 budgeted fixture_runner 接口及摘要/fixture_reproduced 核验，缺证明 gate 拒绝，不伪造生产工具超时。
- 监测接口对所有传入聚合指标强制标记 manual，防止调用者用 source=runtime 冒充自动采集。
- 第一轮全量 825 passed / 2 failed / 1 skipped，失败为短 store.py 引用歧义和 suite_ref 旧全局滞留断言；已修为完整路径及真实任务清单/上下文恢复断言。后续全量分别 829/1、830/1，通过后新增 1 项伪装监测回归，再导出最终候选验证。
