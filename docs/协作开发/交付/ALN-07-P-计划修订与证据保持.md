# ALN-07-P 交付：计划修订与已完成证据保持

最新补审：**运行时七组问题已修复，aafca22待其他审阅者复核**。31项计划及7项传输探针在原源码31失败/7通过；相关207通过，固定源码1602/1。原planner不变量切片的第三方部分接受保持，见[运行时补审](../验收/2026-10-24-ALN-07-P运行时补审.md)。下文保留原交付时点，不把当时未完成的07-W/T/R描述当作当前状态。

任务：ALN-07 / ALN-07-P 计划修订与已完成证据保持
负责人：claude-main
审阅人：待定（须为作者之外的独立复核）
基线 commit：未发布基线；在当前共享候选工作区增量开发（同 ALN-06-A/08-M/11-A 方式），不用旧 HEAD 冒充
分支与最终 commit：main。**提交归属需注意**：任务卡与 workboard 登记经并行会话提交 `af6ef48` 扫入；
代码（`planner.py` +128 / `engine.py` +3）、`tests/test_plan_revision.py`（+206）、本交付文档、
.planning 验证日志与四份台账联动经并行会话提交 `6d55756` 扫入。`6d55756` 标题为「再认领 ALN-17/20」，
ALN-07-P 内容不在其标题所述范围内，系我方暂存文件被扫入；已用 `git show <commit> -- <file>` 逐项
核对上述改动均在内。审阅时请直接核对这两个 commit 的文件内容，勿按提交标题检索。
认领状态：review（待独立复核）

## 触发场景与行为变化

- 输入/触发：计划执行中发生修订（模型重规划、连续失败恢复、剩余子图重排）。
- 修改前：`ExecutionPlan.adjust(new_steps, reason)` 用新步骤列表**重算** `completed_work`。实测四个违约（复现脚本见验证证据）：
  1. 修订丢弃已完成步骤 → `completed_work` 从 `['s1']` 变 `[]`，证据丢失；
  2. 已完成步骤被重发为 pending → 降级成功，续跑可重放成功副作用；
  3. 悬空依赖的修订被静默接受（`validate()` 事后才报）；
  4. 空计划被接受。
- 修改后：`adjust(new_steps, reason, *, level="replace")` 分级修订，不变量违约即抛 `PlanRevisionError`，**计划保持修订前状态**：
  - `completed_work` 单调不减——不得丢弃或降级已完成步骤；
  - 修订后步骤集合须过结构校验（非空、id 唯一、deps 存在、无环）；
  - append 级只允许追加；reauthorize（目标/约束变更）拒收，指向人工授权接口；
  - 修订留痕（级别/原因/增删/时间/证据快照），`to_dict/from_dict` 往返保留。

## 范围

- 独占模块：`evoagent/planning/planner.py`（非共享文件）、`tests/test_plan_revision.py`（14 项）。
- 共享文件补丁与整合角色：`evoagent/react/engine.py::_mark_step_failed` 一处调用加 `level="append"`（runtime 共享文件，按认领卡声明交接；默认级行为不变，无第二套计划权威）。
- 新增/修改契约：`PlanRevisionError`、`REVISION_LEVELS`、`ExecutionPlan.revisions` 字段（旧持久化计划无该字段，`from_dict` 按空列表兼容）。
- 数据迁移：无。`revisions` 为新增可选字段，旧记录读取不受影响。

## 验证证据

- 正常路径：追加/替换/重规划三级修订均按语义提交并留痕；序列化往返保留修订记录与证据。
- 拒绝/失败路径：丢弃已完成步骤、降级已完成步骤、悬空依赖、依赖环、空计划、重复 id、append 删既有步骤、未知级别、reauthorize —— 全部拒绝且计划保持原状（测试锁定）。
- 既有调用方回归：引擎恢复追加形状（`r4` 依赖失败步骤）行为不变；`test_planning/test_control/test_workflow/test_process_recovery` 36 项通过。
- 命令与日志：
  - `python -m pytest tests/test_plan_revision.py -q` → 14 passed（`.planning/2026-10-22-aln07/revision-tests.txt`）
  - `python -m pytest tests/test_planning.py tests/test_control.py tests/test_workflow.py tests/test_process_recovery.py -q` → 36 passed（`.planning/2026-10-22-aln07/regression-targeted.txt`）
  - 全量 `python -m pytest tests -q` → 1109 passed / 1 skipped（共享候选工作区，含他人在途改动，非干净克隆复现）
- 验证边界：全部为进程内单测与真实 `ExecutionPlan` 对象，无 Mock 计划权威、无真实模型、无真机。修订与副作用账本的衔接判据（submitted/unknown 对账）本卡只定义语义，接线属后续。

## 回滚与未完成事项

- 回滚：还原 `planner.py` 与 `engine.py` 一处调用即可回到修订前语义；无数据迁移，无回滚风险。
- 保留的限制与后续：
  - `_risks_for` 静态风险文案仍未转为可执行恢复策略（WP-6.4 剩余）；
  - ALN-07 第一项（工作流匹配：目标/输入/权限/成本/模型版本匹配与执行模式显式选择）与第三项（轨迹编译：输入绑定与恢复分支的候选抽取）待拆卡；
  - 修订与 `core/side_effects.py` 的 submitted/unknown 对账接线待 runtime 线整合。
- 影响的主题/契约：R-04（🔹 维持，缺口收窄）、F-54（🔹 部分）、WP-6.4（部分）；未触及 20 条系统级验收的状态。

审阅结论：待审
整包结论：仍实现中（ALN-07 另两项未开工）
