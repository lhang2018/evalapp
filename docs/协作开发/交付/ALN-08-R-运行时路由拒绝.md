# ALN-08-R：运行时路由拒绝旁路修复

用户复核指出core/controller._route_model吞掉ModelRouter的拒绝，再直接返回react.model，并记录source=router。即使生产默认Mock可用使该路径通常不触发，这仍允许运行时绕过策略。

## 触发及行为

修复前：路由拒绝后，控制器自行选择默认适配器；规划阶段原已选择的模型也可能继续显示为成功归因。

修复后：仅由路由器决定是否可回退。控制器收到拒绝或异常时，清空model_id/provider，写source=router_rejected（ValueError）或router_error（其他异常）及error_class，发model_route_failed事件，再原样抛给现有任务失败/诊断/Run收尾流程。没有增加新任务状态或默认执行分支。

正常路由、路由器批准的可用fallback与合法override不变；未知override仍告警后走常规路由，常规路由拒绝就失败。事件与运行清单只保存错误类别，不复制异常正文。

## 验证

源码提交 `24cd5247ea7e41513f5bfc33c95c3c46ab2fc303`，tree `616520516106b8c5130979f8fb320a5ccf751bc7`。

新增6项专项：旧实现5 failed/1 passed；修复后与运行、迁移、Manifest、资产/模型审阅和路由联合 **100 passed**。测试通过真实App提交验证不可用默认模型、未知override、异常、规划后拒绝、事件/落库与合法fallback；同配置下ModelRoutingEvaluator和RuntimeController均拒绝。

后端干净提交全量结果以workboard本卡evidence及`.planning/2026-10-22-router-rejection/clean-backend-tests.txt`为准。客户端目录Git tree与d77b241完全一致，本轮未重跑；136项及typecheck结果明确沿用d77b241的独立证据，不算本轮新测。

该精确提交干净后端全量 **915 passed / 1 skipped（100.44s）**。候选清单与日志摘要见`.planning/2026-10-22-router-rejection/prepared-baseline.json`；文档/台账与准备证据校验另行记录。

## 状态与保留边界

本卡待独立runtime reviewer审阅，不自批。本轮标注ALN-08-M首轮816/1/2为历史；909/1为上一干净候选结果，后续结果按精确新提交另列。

baseline仍candidate、published_commit仍null；ALN-04/10、本次及此前审阅修复、真机与真实供应商/候选验收继续保留。其他会话的tests/test_e12_e13.py、Android通知与.workbuddy未纳入。
