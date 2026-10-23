# ALN-07-W 交付：工作流匹配与执行模式选择

任务：ALN-07 / ALN-07-W 工作流匹配与执行模式选择（R-03 主题中工作流匹配、执行模式两维度的实现与评测）
负责人：claude-main
审阅人：待定（须为作者之外的独立复核）
基线 commit：未发布基线；在当前共享候选工作区增量开发（同 ALN-06-A/07-P/08-M/11-A 方式），不用旧 HEAD 冒充
分支与最终 commit：main。代码提交 `a2e08a3`（见下方范围与验证证据）。
认领状态：review（待独立复核）

## 触发场景与行为变化

- 输入/触发：规划阶段需根据目标/意图与可用输入、权限、依赖、模型版本、预算，从候选工作流中显式选定执行模式（稳定工作流 / 混合 Agent 工作流 / 动态规划）。
- 修改前：`evoagent/planning/planner.py` 只按意图产出步骤，**没有任何工作流匹配层，也没有执行模式概念**；R-03「路由质量评测与纠错」的正式主题含「工作流匹配」「执行模式」两项，但已落地的 F-53 只评测意图与专家——`routing_coverage()` 中 `workflow` 与 `execution_mode` 两层长期 `evaluated: False`，台账却标 ✅，覆盖与实际不一致。
- 修改后：新增纯读匹配服务 `WorkflowMatcher`（五项检查：输入/权限/依赖/版本/预算），按规则显式选择执行模式并留痕理由，不引入第二套授权/预算权威、不扣账；`PlanningEngine` 透传该能力，`ExecutionPlan` 记录 `execution_mode` 与 `selection_rationale`；`routing_eval` 新增 `WorkflowModeEvaluator` + 内置评测集 + 报告，`routing_coverage()` 两层如实改为已评测。`routing_coverage()` 仍诚实保留「真实运行时尚未自动接线匹配层」的剩余缺口。

## 范围

- 独占模块：
  - `evoagent/planning/contracts.py`（新增，144 行）：`WorkflowCandidate` / `MatchContext` / `CheckResult` / `CandidateVerdict` / `ModeSelection` 数据契约，枚举 `EXECUTION_MODES` / `CHECK_NAMES`。
  - `evoagent/planning/matching.py`（新增，197 行）：`WorkflowMatcher.evaluate/select` 与 `selection_from_dict` 序列化往返。
  - `tests/test_workflow_matching.py`（新增，24 项）。
- 共享文件补丁与整合角色：
  - `evoagent/planning/planner.py`（runtime 共享文件，按认领卡声明交接）：`ExecutionPlan` 增 `execution_mode` / `selection_rationale` 两字段（含 `to_dict/from_dict` 往返，旧计划无该字段按空串兼容）；`PlanningEngine` 增 `set_workflow_candidates` / `select_execution_mode`（`last_selection` 属性）与 `make_plan` 的 `match_context` / `workflow_candidates` 可选参数。**不改既有分级修订语义、不改步骤生成路径**——仅当调用方显式传入上下文时记录模式，否则字段为空。
  - `evoagent/evolution/routing_eval.py`（runtime 共享文件）：新增 `WorkflowModeCase` / `WorkflowModeCaseResult` / `WorkflowModeReport` / `WorkflowModeEvaluator` / `builtin_workflow_mode_dataset`，并把 `routing_coverage()` 的 `workflow` 与 `execution_mode` 两层改为已评测（保留运行时缺口）。既有 `RoutingEvaluator` / `ModelRoutingEvaluator` 行为不变。
- 新增/修改契约：执行模式选择与理由结构（`ModeSelection`）。提供方 = 匹配服务；消费方 = `PlanningEngine` 与调用方（运行时接线属后续）。
- 数据迁移 / 旧格式兼容：无。`execution_mode` / `selection_rationale` 为可选字段，`from_dict` 缺失时按空串；`WorkflowMatcher.select` 返回结构自包含。

## 验证证据

- 正常路径：`WorkflowMatcher` 五项全过 → `stable_workflow`（多候选取最低成本，同成本按 (workflow_id, version) 字典序）；仅差一项且该项是 `dynamic_tolerates` 声明项 → `hybrid_agent`（列出需动态判断节点）；其余 → `dynamic_planning`（理由列出最接近候选缺什么）。`PlanningEngine.make_plan(match_context=..., workflow_candidates=...)` 的 `ExecutionPlan` 记录模式与理由，`to_dict/from_dict` 往返保持。
- 拒绝/失败路径（逐项原因可查，不静默降级）：
  - 缺输入 / 权限 / 依赖 / 版本 / 预算 → 各自 `CheckResult.ok=False` 且 `missing` 或 `detail` 说明缺什么；
  - **预算不可知时不得假设充足** → 直接拒绝并写「预算不可知」；
  - 无候选 → `dynamic_planning`，`workflow_id=""`（无隐式默认）；
  - 多缺且无可容忍项 → `dynamic_planning`，理由列缺项。
- 确定性：候选顺序、输入顺序都不改变选择与理由（frozenset + 排序 + 最小键）。
- 预算不越权：`select` 前后 `MatchContext.remaining_budget` 不变；匹配器不持有任何预算权威（断言无 `budget_controller`）。
- 评测覆盖：`builtin_workflow_mode_dataset()` 7 例覆盖三模式与各项拒绝原因，`WorkflowModeEvaluator.run` 给出 `mode_accuracy`（稳定 1.0）与 `workflow_accuracy`、`by_mode`；并设反向用例证明评测有判别力（故意改错期望模式 → 准确率下降）。`routing_coverage()` 两层现 `evaluated: True`。
- 命令与结果：
  - `python -m pytest tests/test_workflow_matching.py -q` → **24 passed**（新）
  - `python -m pytest tests/test_planning.py tests/test_plan_revision.py tests/test_routing_eval.py tests/test_routing_eval_api.py tests/test_workflow_matching.py tests/test_evolution.py tests/test_evolution_benefit.py -q` → **123 passed**（含被本卡更新的 2 个覆盖断言测试）
  - 既有断言「workflow/execution_mode 未评测」的测试已更新为「已评测 + 仍保留运行时缺口」，保持诚实约束。
- 验证边界：全部为进程内单测与真实 `WorkflowMatcher` / `ExecutionPlan` 对象，无 Mock 匹配权威、无真实模型、无真机。`routing_coverage()` 的断言（`WorkflowMatcher.select` 可调用）作为导入期自检，避免覆盖表悄悄过期。

## 回滚与未完成事项

- 回滚：还原 `contracts.py` / `matching.py` / `planner.py` 本卡补丁、`routing_eval.py` 本卡补丁即可；无数据迁移，无回滚风险。
- 保留的限制与后续：
  - **真实运行时尚未自动接线匹配层**——本卡只交付可被调用方显式调用的服务与评测，主链按目标自动选工作流/模式属后续范围（任务卡 §实施顺序第 5 步）。
  - R-03 其余已知缺口（如生产模型评测集、未知成本只计数不折算）不在本卡范围，由 F-53 既有条目跟踪。
  - ALN-07 整包仍实现中：`_risks_for` 静态风险文案未转为可执行恢复策略、轨迹编译（输入绑定与恢复分支候选抽取）未开工。
- 影响的主题/契约：R-03（缺口收窄，workflow/execution_mode 维度补评测；仍 🔹 部分，因运行时未接线）；F-53（评测维度扩展）；未触及 20 条系统级验收的状态。

## 审阅结论：待审（须作者之外独立复核）
## 整包结论：仍实现中（ALN-07 另两项未开工）
