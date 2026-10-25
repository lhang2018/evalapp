# ALN-23-A 交付：统一任务契约对外模型

任务：ALN-23-A 统一任务契约对外模型（PRODUCT-2026-10-25，父包 ALN-23）
负责人：zcode-main（2026-10-25 接手，原认领 codex-runtime 未开工）
审阅人：待定（须独立复核，不接受自审自收）
基线 commit：`5e3972e`（接手锚点；基线仍 candidate）
分支与最终 commit：feature/user-direction `0fda873`（三入口同源+general修复）+ `9862a79`（契约文档）+ `33e84e5`（用例证据）
认领状态：delivered（review，待独立复核）

## 触发场景与行为变化

- 输入/触发：自然语言目标/约束/产物/验收条件需要经 REST/CLI/客户端三
  入口以同源字段进入 Task 权威。
- 修改前：REST 已有全字段但 CLI/客户端缺约束/验收条件等字段；
  general 提示无条件屏蔽真实分类（客户端兜底 general → 分类器不跑）。
- 修改后：三入口同源收同一契约字段集；general 不再短路——交给分类器
  确认或覆盖（回落仍 general，语义不变）；显式具体 intent 仍短路。

## 范围

- `evoagent/core/controller.py`：`_classify` general 不短路修复。
- `evoagent/cli.py`：`task` 命令补齐 `--constraint/--acceptance/
  --required-tool/--required-permission/--workflow-suitable`；
  `cmd_task` 支持注入 app 便于测试。
- `android-app/src/offline/types.ts`：`TaskContractInput` +
  `LocalTask.contract` 可选字段。
- `android-app/src/offline/tasks.ts`：`submit` 与 `TaskTransport.submitTask`
  签名扩展。
- `android-app/src/offline/apiTransport.ts`：contract 透传。
- `android-app/src/api.ts`：`submitTask` 契约字段进 body。
- `android-app/App.tsx`：23-B 模式判定/任务行视图/能力卡/反馈入口接线
  （四卡一并上屏）。
- 契约文档：`docs/协作开发/设计/ALN-23-任务契约对外模型.md`。

## 验证证据

- `test_task_contract_entries` 7 项：REST 全契约持久化（constraints/
  acceptance/required_tools/permissions/open_questions/expected_output/
  target_object/complexity 逐字段断言）、general 不屏蔽（笔记→
  note_taking）、显式短路保持、真 general 保持、非法 complexity 400、
  CLI/REST 逐字段同源、cmd_task 注入。
- 合并回归：test_capability_status + test_feedback = 11 过。
- 客户端 272 全绿 / tsc 0 错误。
- 全量候选：21899e6 → 1701/1 + 268 + tsc 0 错（含本卡前序提交）。
- general 修复后：关键词分类器正常工作（笔记→note_taking），真 general
  目标不受影响。

## 范围与缺口

- 客户端聊天面板 UI（chat turns 展示与 seed 构建触发）属后续 UI 卡，
  `TaskContractInput` 签名已就位。
- 不改 Task 状态机/执行内核语义。
- 通知在途文件不触碰。

## 回滚与未完成事项

- general 不短路修复可 revert 独立回退；CLI/客户端新增均为可选参数。
- 未完成：23-A 整包关闭待独立复核 + 客户端 UI 后续卡。

审阅结论：待审
整包结论：仍实现中
