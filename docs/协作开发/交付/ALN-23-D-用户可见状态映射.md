# ALN-23-D 交付：用户可见状态与权威任务状态映射

任务：ALN-23-D（PRODUCT-2026-10-25，父包 ALN-23）
负责人：zcode-main；审阅人：待定；基线：`5e2b7e3`
分支/commit：feature/user-direction `b40863c`（映射层）+ `d1272c9`（App 接线）
认领状态：delivered（review，待独立复核）

## 行为变化
- taskViewOf 单一映射：服务端 13 态 + 本地 6 阶段 → 7 种用户视图
  （queued/working/needs_you/waiting/done/failed/cancelled）。
- 等待原因来自权威字段（waiting_approvals/waiting_request/
  NEEDS_RECONCILE/last_sync_error），终态清零，已答澄清不追问。
- 防伪造：完成只认服务端终态；模型文字无效；本地 DONE 标注"（本地）"。
- App 任务行改用视图 label + 等待原因行（d1272c9）。

## 验证
- taskview.test 5 项：逐态映射/终态判定/等待原因/离线阶段/暂停恢复。
- 客户端 272 全绿，tsc 0 错误。

## 范围与缺口
- 状态来自 Task/事件权威；模型文字不伪造完成或进度（验收红线）。
- 不建立第二套状态；映射为只读投影。

审阅结论：待审
