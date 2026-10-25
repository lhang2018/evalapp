# ALN-02-P 真实供应商副作用与进程故障恢复

任务：ALN-02-P / 真实供应商副作用与进程故障恢复
负责人：dsh-lead（本会话认领）
审阅人：待定（未分配独立审阅人，不自批为独立验收）
基线 commit：`a038f2195ab8bc5d571900fdf27a14e1400adf5b`（认领时的 `feature/user-direction` HEAD，不是已发布共同基线）
分支与最终 commit：`feature/user-direction` / `b63f85e213cffb2529eaf07355cebcee7681c952`（tree `739760f0b13b9d34e99ef20a6c2b51022e2cbdb8`）
认领状态：claimed → **review**

## 触发场景与行为变化

- 输入/触发：
  - `ToolRegistry.execute()` 调用**外部 HTTP 供应商**工具（`vendor.submit`）；
  - 外部调用超时/响应丢失后的对账请求 `ToolRegistry.reconcile_effect()`；
  - 进程在「已提交、未确认」窗口退出后的重启恢复。
- 修改前：
  - 02-A 已交付副作用预记账（`begin`/`finish`/`bind_operation`/`accept_receipt`）、`reconcile_effect` 契约与 fencing；但所有回执/对账验证只跑在测试假函数上，**没有任何真实外部适配器**消费这条链路——真实供应商的幂等提交、权威回执查询、进程退出窗口都未验证（09-F 交付亦注明真实外部调用待验）。
- 修改后（新增 `evoagent/tools/vendor_http.py`，未改动既有 shared_files 中的任何文件）：
  - `VendorClient`：最小 HTTP 客户端（`POST /effects` 提交、`GET /receipts/{key}` 查权威回执），带超时与鉴权头；协议错误/不可解析回执/幂等键不一致一律抛 `VendorError`，不猜测外部状态。
  - `VendorEffectAdapter`：把供应商注册成副作用工具——
    - `runner`：由**参数规范化摘要**派生客户端幂等键（跨进程/重启稳定，与 registry 默认幂等键同源），提交后返回带 `evidence.external_receipt_ref` 的执行结果；网络/超时异常向上抛出，由 registry 按 `side_effect_level=external` 标记 `side_effect_uncertain`、`required_action=reconcile`，且 `retry_policy=0` 不自动重试。
    - `reconcile`：按同一幂等键向供应商**重新查询权威回执**（不信任调用方传入），存在且一致 → `independently_verified=True` + `executed=True`；明确不存在 → `independently_verified=True` + `executed=False`（证明未执行，账本转 failed，允许一次全新尝试）；查询失败或回执与权威记录不一致 → 抛错保持 unknown。
  - 契约：`side_effect_level=external`、`idempotency_policy=key`、`retry_policy=0`、`requires_network=True`、输入/输出 JSON Schema、`reconcile=self.reconcile`。

## 范围

- 独占模块：`evoagent/tools/vendor_http.py`（新增）。
- 共享文件补丁与整合角色：**无**。本卡 shared_files 中登记的 `core/controller.py`、`core/task.py`、`core/side_effects.py`、`tools/registry.py` 等**一行未改**——预记账/超时/幂等/对账全部复用 02-A 已接受切片。
- 新增/修改契约：新增工具契约 `vendor.submit`（外部副作用级），既有 `reconcile_effect` 契约的消费方从假函数变为真实 HTTP 适配器；无数据库 schema 变更、无迁移。
- 数据迁移、旧格式兼容或明确拒绝：无迁移；供应商回执缺失/不可解析/不一致时 fail-closed（保持 unknown），不降级为成功。

## 验证证据

- 正常路径及实际产物：
  - 真实本地 HTTP 服务（`ThreadingHTTPServer`，按幂等键去重并保存权威回执）下：`ToolRegistry.execute` 一次成功、供应商**只执行一次**、`evidence.external_receipt_ref=vendor:rcpt-N`；同参数同幂等键再次执行走账本缓存重放（`evidence.replayed=True`），供应商执行次数不变。
- 拒绝/失败/重复/并发/重启：
  - 响应丢失（客户端 0.2s 超时、供应商已记录副作用）：结果 `side_effect_uncertain`、账本 unknown、`required_action=reconcile`；未对账前重试被 `side_effect_unknown` 拒绝且供应商执行次数不变；按权威回执对账后 confirmed，再执行只重放缓存。
  - 伪造回执（`receipt_id` 与供应商权威记录不符）：`reconcile_effect` 抛 `VendorError`，账本保持 unknown；真实回执才放行。
  - 供应商明确无回执（请求体不可解析、未记录副作用）：对账得到 `independently_verified=True, executed=False`，账本转 failed，允许一次全新尝试并只执行一次。
  - **进程故障**：独立子进程在供应商已收到请求、响应未返回的窗口被强杀（`kill`），账本停在 submitted/unknown（绝不当成未执行）；重启后用新连接按供应商权威回执对账 → confirmed，同一幂等键只重放缓存，**全程供应商执行次数 = 1**。
  - 供应商按幂等键去重：同键重复提交返回同一 `receipt_id`，只执行一次。
- 命令、结果、日志与对应源码 commit：
  - 提交 `b63f85e213cffb2529eaf07355cebcee7681c952`；
  - **验证在干净 detached worktree 中进行**：`git worktree add --detach <tmp> 2ea7d38`（HEAD）+ 仅拷入本卡两个新文件，避免主工作区他人未提交改动干扰：
    - 本卡专项：`python -m pytest tests/test_vendor_side_effects.py -q` → **6 passed**；
    - 相关域回归（vendor + aln02_receipts + side_effects + plan_side_effect_reconcile）→ **60 passed**；
    - 干净全量：`python -m pytest tests/ -q` → **1803 passed / 2 failed / 1 skipped（442.82s）**，日志 `.planning/2026-10-25-aln02p/backend-clean-worktree.txt`。
  - 两项失败**与本卡无关且在该干净 HEAD 上本就存在**（把本卡两个新文件删掉后单独复跑，两项仍失败）：
    - `tests/test_ledger_drift.py::TestNoDrift::test_all_ledgers_have_no_drift`：`GAP_ANALYSIS.md:177` 裸引用 `cli.py:232` 在 HEAD 已漂移（`tools/scan_all_ledgers.py` 报 1 条 `bare_points_to_blank`）；
    - `tests/test_runtime_reservations.py::test_explicit_hint_does_not_allocate_a_phantom_classifier_call`：预算/预留线的已提交回归（他人改动，未在本卡范围）。
- **主工作区未能复跑的原因（诚实记录）**：会话期间他人对 `evoagent/tools/registry.py` 有未提交改动（`self.authorizations.check(..., principal=...)`），而 `core/authorization.py::AuthorizationStore.check()` 尚不接受该参数，任何工具执行路径都会 `TypeError`；这是他人正在进行的在途改动，未由本卡修改，也未在本卡提交中带入。
- Mock / 本地组件 / 真机 / 真实供应商的验证边界：
  - 对接的是**受控外部测试服务**（真实本地 HTTP 服务，具备幂等键去重与回执查询），不是生产供应商；生产接入需替换 `base_url`/鉴权头/供应商错误码映射，并补充供应商侧 SLA 与回滚核对（见保留缺口）。
  - 进程故障用真实子进程强杀验证本地恢复路径，不是多机部署或生产调度环境。

## 回滚与未完成事项

- 回滚触发、操作及验证：`git revert b63f85e213cffb2529eaf07355cebcee7681c952`（只删除新增适配器与测试，无 schema/迁移/既有行为依赖）。回滚后 `tests/test_vendor_side_effects.py` 整体消失，02-A 的既有对账能力不受影响。
- 保留的限制、后续 ALN 和依赖：
  - 未接真实供应商（凭证、费率、供应商错误码、限流与重试语义需按供应商合同适配）；
  - 未做补偿（compensation）真实外部调用：`compensated` 状态仍只由既有对账/门禁驱动，真实退款/撤销调用属原包后续；
  - 未做并发压测与多机时钟校准；幂等键派生依赖参数规范化（`default=str` 覆盖不可序列化值，键稳定性已限本适配器参数域）；
  - 未接 App/CLI/API 装配（不在本卡写入边界），适配器由工具注册方按现有 `register(contract, runner)` 装配。
- 影响的主题/实体/ADR/契约/系统验收条目：新增工具契约 `vendor.submit`；副作用账本与 `reconcile_effect` 语义不变；ALN-02 整包关闭仍需真实供应商接入与组合进程故障注入（本卡只证明适配器与恢复语义）。

## 审阅结论：待审

## 整包结论：仍实现中（ALN-02：真实供应商/生产环境与补偿链仍待验；本卡交付真实外部适配器与崩溃恢复语义）
