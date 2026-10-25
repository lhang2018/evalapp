# ALN-23-C 交付：任务工作区与真实计划进度

任务：ALN-23-C（PRODUCT-2026-10-25，父包 ALN-23，阶段1）
负责人：zcode-main；审阅人：待定（须独立复核）
基线 commit：`5de4d00`
分支与最终 commit：feature/user-direction `da85ab7`
认领状态：delivered（review，待独立复核）

## 行为变化

- `workspaceFromCheckpoint`：从权威 checkpoint（SQLite 落盘）投影 DAG
  节点状态为六种可辨视图（succeeded/pending/blocked/running/failed/skipped），
  节点依赖关系保留。
- 无依据不显示百分比：仅全部节点有终态时显示 100%。
- 用户待办（审批/澄清/重提）投影为 `pendingActions`。
- 部分成果（已完成节点输出）在 `partialResults` 中可见。
- blocked 仅由服务端权威数据判定，客户端不基于 deps 推断。

## 范围

- 独占新增：`android-app/src/features/workspace/view.ts` + 8 项测试。
- 不改 server.py/routes.py/App.tsx（App 工作区 UI 挂载在 24-A 消费）。
- 不改执行内核语义。

## 验证

- workspace.test 8 项全过：六态/依赖/blocked推断移除/部分成果/用户待办/
  空checkpoint/全部完成/百分比无虚报。
- 客户端 280 全绿 / tsc 0 错误。

## 范围与缺口

- App 工作区 UI 挂载（任务详情页展示 DAG 进度）属 ALN-24-A 消费范围。
- 真机证据按 ALN-12-V 真机矩阵另采。

审阅结论：待审
整包结论：仍实现中
