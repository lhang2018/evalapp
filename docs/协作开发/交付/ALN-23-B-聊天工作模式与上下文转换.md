# ALN-23-B 交付：聊天与工作模式及上下文转换

任务：ALN-23-B（PRODUCT-2026-10-25，父包 ALN-23）
负责人：zcode-main；审阅人：待定；基线：`5e2b7e3`（非发布基线）
分支/commit：feature/user-direction `d8aa054`（逻辑层）+ `d1272c9`（App 接线）
认领状态：delivered（review，待独立复核）

## 行为变化
- classifyMode：显式切换 > 输入特征 > 默认聊天；概念问答（什么是/为什么/
  如何/帮我解释…）保持轻量聊天，即使提到文件/运行词；复合执行句
  （"解释…然后帮我生成报告"）判 work。
- buildTaskSeed：聊天→工作转换投影。goal 取最后非约束用户消息（新目标
  覆盖旧纠正）；仅 confirmed 升级为约束；corrections 留存防重犯；
  completed 只进摘要。
- noRepeatGuard：已完成要点绝不重复执行；assistant 讨论结论不产生待办。
- App 接线：submitGoal 前置模式判定（d1272c9）。

## 验证
- modes.test 7 项 + developedReview.test 扩展验收 4 项（外部会话投递，
  初跑 4 失败，按其规格修正 classifyMode/goal 选择/waitReasonOf 后全过）。
- 客户端 272 全绿（含全部既有回归），tsc 0 错误。

## 范围与缺口
- App 接线按设计稿（docs/协作开发/设计/P0四卡App接线设计稿_2026-10-25.md）
  仅模式徽标+种子就绪；聊天轮次面板属后续 UI 卡，种子当前来自模块输入。
- 不改执行内核；App.tsx 为共享文件串行合入。

审阅结论：待审
