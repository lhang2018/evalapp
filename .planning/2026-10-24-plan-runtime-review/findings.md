# 发现

- HEAD50dd94c，无新业务提交、无CodeGraph。07-P剩余engine/恢复路径待审，其他第三方接受保持。
- _current_step仅取列表第一个pending/running，不检查deps；_mark_step_failed未核对tool_hint。将以实际ReactEngine和ToolRegistry负向测试确认。
- 首轮21项原代码20失败/1通过，两个真实App用例显示：失败依赖照常执行、损坏计划退化为None后执行，最终均能SUCCEEDED。
- 修复：ExecutionPlan统一运行快照校验；Controller存在损坏计划不再静默降级；引擎依赖就绪选择、plan_blocked在控制器SUSPENDED、计划完成后拒绝额外工具；完成进度要求ok/executed/contract_valid。
- 失败计数按步骤/工具/错误分组、真实成功清理，未关联工具不能标当前步骤failed；恢复时刷新单一计划上下文槽，保留真实执行trace，不堆积旧提示。
- 最终24项新测试；相关156通过（含process_recovery、run_continuation、execution_boundaries与07-E回归）。不重做账本或授权，不增加自动修订/恢复权限。

- e54ad2a固定源码首轮全量1失败/1589通过/1跳过；单独再次复现Goal域权限拒绝未消费POST体，Windows客户端ConnectionAbortedError。保留first-attempt-e54ad2a.json及逐项日志，不称全量绿。使用同一有界丢弃逻辑修复注册路由401/403，并用惰性读取标志避免已消费体二读；49项接口回归通过。
- 多分支探针扩展至failed/skipped/pending/running前置依赖；有独立就绪分支也不放行全部候选均未就绪的工具。同工具多步骤无显式动作身份仍不可精确归因，不宣称补齐step_bindings接线。

- 扩展“未就绪工具”拦截时相关回归发现7失败/1线程错误：旧默认计划的无tool_hint准备步骤可合法使用后续同名工具。已修正：仅在当前步骤已有工具绑定时拒绝其他未就绪分支；未绑定准备步骤保留兼容，但失败/跳过祖先分支仍拒绝。新增同工具多步骤与通用准备的正向控制，旧测试未修改。中间失败保留related-unbound-probe.txt。

- 最终38项注入原源码31失败/7通过；相关207通过，路由兼容补丁88通过。固定aafca22全量1602/1及五检查通过；同tree复用客户端166/typecheck。报告/台账更新但不自批、无新开发卡、未发布。
