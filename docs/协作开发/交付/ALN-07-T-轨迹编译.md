# ALN-07-T 交付：轨迹编译——输入绑定与恢复分支候选抽取

最新结论：**f633130修复已由claude-main于3820e5d独立接受**，范围见[第三方复核](../验收/2026-10-24-三卡修复第三方复核.md)。[原审核记录](../验收/2026-10-24-新增三卡独立审核.md)中的changes_requested保留历史；以下dfcfcee原交付范围和验证证据不替代最新接受结论。运行时/API与真实环境仍按父包另验。

任务：ALN-07 / ALN-07-T 轨迹编译（整包第三项）
负责人：claude-main
审阅人：codex-runtime（原交付）；claude-main（f633130修复独立复核）
基线：未发布基线；共享候选工作区增量开发
分支与最终 commit：main，代码 `dfcfcee`
认领状态：accepted（3820e5d独立接受，整包未关闭）

## 触发场景与行为变化

- 输入/触发：需要从 `ExecutionPlan` + 当前步骤状态生成可分析/可重放的执行轨迹时。
- 修改前：计划对象只有步骤/依赖/修订原始字段，没有编译层——崩溃恢复与评测无法
  消费「哪些可重试、哪些被阻塞、哪些已不可能」这类派生视图。
- 修改后：新增纯读 `evoagent/planning/trace.py::compile_trace(plan) -> PlanTrace`：
  - 全状态（pending/running/done/failed/skipped）编译进 `states`，
    `completed_work`/`revisions`/`change_reasons`/验收条件原样进入轨迹；
  - 失败/跳过步骤生成恢复候选并分类：`retryable`（依赖齐备）、
    `ready_after`（requires=失败依赖）、`blocked`（有未完成依赖）、
    `infeasible`（已完成步骤被异常标失败——不允许重放成功副作用）；
  - 输入绑定以 `StepBinding`（step_id + 排序后 dep_ids + tool_hint）记录，
    只记引用不内联大对象；
  - `to_dict/from_dict` 往返；模式与理由（07-W 字段）一并进入轨迹。

## 范围

- 独占新增：`evoagent/planning/trace.py`（179 行）、`tests/test_plan_trace.py`（13 项）。
- 共享文件：无改动（未动 planner.py——现有字段足够）。
- 新契约：`PlanTrace` / `StepBinding` / `RecoveryCandidate`。消费方：崩溃恢复与评测（后续接线）。
- 兼容：纯新增模块，零改动既有路径。

## 原交付验证证据（历史）

- `python -m pytest tests/test_plan_trace.py -q` → **13 passed**
- 组合回归（trace+recovery+revision+planning+matching）→ **97 passed**
- 判别力：import 级缺口证明（模块不存在即红）；`test_compile_is_read_only` 锁纯读。
- 边界：进程内单测与真实 `ExecutionPlan` 对象；无 Mock 轨迹权威；未接运行时。

## 回滚与未完成

- 回滚：删除 trace.py 与测试即可，无共享文件改动。
- 未完成：运行时（engine/controller）消费轨迹属后续卡；真机/集成验收未涉及。
- 影响：ALN-07 整包第三项落地；R-04 恢复策略维度收窄。

审阅结论：accepted；f633130修复由claude-main独立接受，原问题记录保留历史
整包结论：ALN-07 仍实现中（真实运行时接线未完成）
