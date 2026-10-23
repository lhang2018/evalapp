# ALN-07-R 交付：可执行恢复策略（_risks_for 结构化）

最新结论：**f633130修复已由claude-main于3820e5d独立接受**，范围见[第三方复核](../验收/2026-10-24-三卡修复第三方复核.md)。[原审核记录](../验收/2026-10-24-新增三卡独立审核.md)中的changes_requested保留历史；以下dfcfcee原交付范围和验证证据不替代最新接受结论。运行时/API与真实环境仍按父包另验。

任务：ALN-07 / ALN-07-R 可执行恢复策略（WP-6.4 剩余）
负责人：claude-main
审阅人：codex-runtime（原交付）；claude-main（f633130修复独立复核）
基线：未发布基线；共享候选工作区增量开发
分支与最终 commit：main，代码 `dfcfcee`
认领状态：accepted（3820e5d独立接受，整包未关闭）

## 触发场景与行为变化

- 修改前：`_risks_for(intent)` 返回静态中文文案，消费者无法据此执行任何动作；
  文案与语义两处维护。
- 修改后：
  - 新增 `RecoveryPolicy` 契约（category/action/applies_to_intent/params/rationale，
    action ∈ require_approval/partial_retry/checkpoint/fallback_tool）；
  - `_risks_for` 改为从 `_recovery_policies_for` 渲染——**文案单一来源**，
    兼容读法不变（`plan.risks` 仍是字符串列表）；
  - `ExecutionPlan` 增 `recovery_policies` 字段（旧计划 `from_dict` 按空兼容）；
  - 新增纯读查询 `plan.recovery_for_failure(step_id)`：只对 failed 且不在
    completed_work 的步骤返回策略——done 步骤返回空（不重放成功副作用），
    未知/待定步骤返回空；
  - 已知意图语义与原文案一致（note_taking/mobile_operation →
    side_effect_requires_approval/require_approval；comic_drama →
    high_cost_partial_retry/partial_retry）；未知意图空策略不隐式默认。

## 范围

- 共享文件小改：`evoagent/planning/planner.py`（+69 行：契约、字段、渲染、查询；
  未改修订分级语义与步骤生成路径）。
- 独占新增：`tests/test_recovery_policy.py`（12 项）。
- 兼容：`recovery_policies` 可选字段，旧持久化计划读取不受影响。

## 原交付验证证据（历史）

- `python -m pytest tests/test_recovery_policy.py -q` → **12 passed**
- 组合回归 → 97 passed（trace 13 + recovery 12 + planner 既有全绿）
- 判别力：`test_done_step_never_gets_policy` 锁「不重放成功副作用」；
  `test_legacy_plan_without_field` 锁旧格式兼容；`test_query_is_read_only` 锁纯读。
- 边界：进程内单测；未接运行时自动执行恢复（本卡不宣称）。

## 回滚与未完成

- 回滚：还原 planner.py 本卡补丁即可；字段可选无迁移。
- 未完成：运行时按策略自动执行恢复（require_approval/partial_retry 的执行器）
  属后续卡；`fallback_tool`/`checkpoint` 两类动作暂无意图产出（契约预留）。
- 影响：ALN-07 第二项（WP-6.4 剩余）落地；R-04 维度收窄。

审阅结论：accepted；f633130修复由claude-main独立接受，原问题记录保留历史
整包结论：ALN-07 仍实现中
