# ALN-20-A：Runtime 适配协议与固定版本 PoC 设计

2026-10-24最新验收：**按用户明确授权，由codex-runtime统一复验通过既定切片（accepted）**，自己的修复不伪称第三方；范围、修复、真机证据归属和父包后续见[统一验收](../验收/2026-10-24-全部待审核统一验收.md)。下文保留原交付/任务时点。

当前负责人：codex-runtime，串行排队未开工。ADR-010 要求「模型适配不等于 Runtime」；本卡先交协议、兼容矩阵与 PoC 设计，固定外部 Runtime commit 的实际 PoC 另卡。

## 目标

现状：执行权威在 `evoagent/core/controller.py` 与 `evoagent/react/engine.py`，没有独立的 Runtime 适配协议；`harness/*` 仅作为诊断组件名出现（`evoagent/evolution/diagnostics.py`），未绑定固定版本或能力矩阵。本卡定义保持内核权威的 Runtime 协议（工具/事件/取消/恢复/沙箱五类能力），声明缺能力时的受控拒绝，并给出 Mock/现有运行时两路契约测试设计。

## 写入边界

可独占新增：`evoagent/runtime/`（协议与适配接口，纯契约，不接管执行）、`tests/test_runtime_protocol.py`、`docs/协作开发/交付/ALN-20-A-*.md`。

本卡不改 `evoagent/core/controller.py`、`evoagent/react/engine.py`、`evoagent/workflows/engine.py` 的执行路径，不引入第二条工具/账本权威；这是本卡范围边界，不表示这些文件仍被历史会话占用。固定外部 Harness commit 的 PoC 与 ADR 正式条目另卡。

## 验收

- 协议覆盖工具调用、事件流、取消、恢复、沙箱边界五类能力；每类定义能力声明、错误与拒绝语义。
- 同一契约在 Mock 与现有运行时适配层通过同一组夹具；替换 Runtime 不改变权限、副作用账本或 Task 权威。
- 能力不足时明确拒绝，不静默降级为模型 API 适配。
- 输出兼容矩阵（能力 × 实现）与采用/不采用判据；实际 PoC 结论留待后续卡。

## 交付

协议、适配接口、夹具、兼容矩阵与命令日志，使用[统一交付模板](../交付模板.md)。本卡不宣布 ALN-20 整包完成。
