# ALN-20-A 交付：Runtime 适配协议与固定版本 PoC 设计

2026-10-24最新验收：**按用户授权由codex-runtime统一复验通过既定切片（accepted）**，自己的修复不伪称第三方，见[统一验收](../验收/2026-10-24-全部待审核统一验收.md)。下文保留原交付时点及边界。

任务：ALN-20-A Runtime 适配协议与固定版本 PoC 设计
负责人：zcode-main（2026-10-24 按队列认领开工）
审阅人：待定（须独立复核，不接受自审自收）
基线 commit：`5c90bee`（实际接手锚点；基线仍 candidate，`published_commit` 为空）
分支与最终 commit：main，本卡代码与台账随本交付提交（见 git log "feat(ALN-20-A)"）
认领状态：delivered（review，待独立复核）

## 触发场景与行为变化

- 输入/触发：外部 Runtime（固定 commit 的 Harness 等）需要接入时的
  能力评估与接入契约；或替换内置执行面时保证权限/账本/Task 权威不变。
- 修改前：执行权威在 controller/react engine，无独立 Runtime 协议；
  harness 仅作为诊断组件名出现，未绑定能力矩阵；无法声明"一个实现
  具备哪些 Runtime 能力、缺什么必须拒绝"。
- 修改后：`evoagent/runtime/` 提供五类能力协议（工具调用/事件流/取消/
  恢复/沙箱边界），每类有声明、错误与拒绝语义；Mock 与现有运行时
  适配层通过同一组契约夹具；能力缺失抛
  `CapabilityNotSupportedError` 显式拒绝，不静默降级为模型 API 适配。

## 协议要点（ADR-010：模型适配不等于 Runtime）

- `RuntimeCapability` 五类：`tool_invocation / event_stream / cancel /
  recovery / sandbox`；`declarations()` 逐类声明（supported + details）。
- 拒绝语义：每个能力方法入口先 `require()`——未声明或不支持即
  `CapabilityNotSupportedError`（信息含能力名、Runtime 名、原因、
  "不静默降级"提示）。领域状态用结果对象（`CancelOutcome` 等），
  不走异常流；权威自身的异常原样透传。
- 兼容判定与升级路径见 `evoagent/runtime/matrix.py`：能力 × 实现矩阵
  （mock / existing / model_api_only 参照行）+ `assert_matches_declarations`
  保证矩阵与运行时声明一致（文档与代码不漂移）。

## 兼容矩阵（能力 × 实现）

| 能力 | mock | existing（现有运行时适配层） | model_api_only（参照行） |
|---|---|---|---|
| tool_invocation | full（内存工具表） | full：ToolRegistry.execute，权限/预算/幂等/副作用账本语义不变 | absent：无工具注册表/账本权威 |
| event_stream | full（内存事件表） | full：EventBus subscribe/emit/query_run 直通 | partial：仅会话内消息记录 |
| cancel | full（已知任务幂等，未知 ok=False） | full：controller.cancel_task 协作式/幂等/终态拒绝 | absent |
| recovery | full（内存恢复台账） | full：RunManager.begin(recovery=True)，RUNNING 中断接管 | absent |
| sandbox | **absent（刻意）**：Mock 不做真实隔离，从不执行候选代码 | full：evolution SandboxRunner，一次性目录/无网络/超时强杀 | absent |

采用/不采用判据（固定外部 Runtime commit 的 PoC 另卡按此裁决）：
- AC-1 五类能力全部 full 或有显式补位方案（缺沙箱可注入既有
  SandboxRunner；核心能力只能用模型 API 语义模拟的，不采用）。
- AC-2 权威不被复制：接受注入既有 ToolRegistry/EventBus/Controller 并
  按协议透传的采用；自带第二套工具账本/Task 状态机且无法关闭的不采用。
- AC-3 共享契约夹具（tests/test_runtime_protocol.py）在外部 Runtime 上
  全部通过；任一声明能力行为与声明不符的不采用。
- AC-4 固定 commit 与摘要可复现；只能跟随浮动版本的不采用。

## 范围

- 独占新增：`evoagent/runtime/`（`__init__/protocol/mock/adapter/matrix`，
  纯契约，不接管执行）、`tests/test_runtime_protocol.py`（8 项）、本
  交付文档。
- 契约提供方：evoagent.runtime；消费方：固定外部 Runtime PoC（另卡）、
  诊断组件（harness 命名与能力矩阵对齐属后续）。
- 权威不变性（测试断言）：适配层持有的 registry/bus/controller/runs
  与注入实例同一（`assertIs`）；工具调用未注册返回权威原样
  `ok=False`；取消/恢复语义与 controller/RunManager 逐字对齐；协议包
  内无任何工具账本或 Task 状态副本。
- 本卡不改 controller.py / react/engine.py / workflows/engine.py 执行
  路径；不引入第二条工具/账本权威；通知在途改动不触碰。

## 验证证据

- 共享夹具双实现：`run_contract_suite` 在 Mock 与现有运行时适配层执行
  同一段断言（声明完整、矩阵一致、事件收发与按 run 查询、未知任务
  ok=False、可取消任务幂等、恢复返回新 run_id、沙箱 executed/rejected
  按声明分流）。
- 拒绝语义：Mock 沙箱显式拒绝（不做假执行）；缺组件的现有适配层对
  cancel/recovery/sandbox 逐一 `CapabilityNotSupportedError`；注入
  bus 后事件能力恢复可用（声明与行为一致）。
- 恢复透传：失联运行模拟（test_run_ownership 的 abandon 范式：租约
  过期 + 带预算检查点）后 `recover_run` 走真实 `RunManager.begin(
  recovery=True)` 接管成功；不可恢复状态时权威的 `LeaseBusy/
  RecoveryBlocked` 原样透传（适配层不吞不改）。
- 命令与结果：`python -m pytest tests/test_runtime_protocol.py -q` →
  **8 passed**；全量 `python -m pytest tests -q` → **1634 passed /
  1 skipped**（上一交付 1626 + 本卡 8）。
- Mock/真实边界：Mock 沙箱从不执行代码（契约假件，返回 ok=False 并
  指向真实 SandboxRunner）；现有运行时段使用真实 App/注册表/EventBus/
  控制器/租约/沙箱。固定外部 Harness commit 的实际 PoC、ADR 正式条目
  与真实环境结论按任务卡原范围另卡。

## 回滚与未完成事项

- 回滚：纯新增包 + 新测试文件，revert 单提交即可，无数据/接口迁移。
- 未完成（按任务卡原范围保留）：固定外部 Runtime commit 的实际 PoC
  与 ADR-010 正式条目；harness/* 诊断组件与协议能力矩阵的绑定；
  model_api_only 参照行对应的具体外部实现评估。
- 影响主题：ADR-010、PRD 执行内核边界；ALN-20 整包仍开放。

审阅结论：待审
整包结论：仍实现中
