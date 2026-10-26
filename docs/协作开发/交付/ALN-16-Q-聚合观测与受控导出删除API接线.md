# ALN-16-Q 聚合观测与受控导出删除 API 接线

任务：ALN-16-Q / 聚合观测与受控导出删除 API 接线（阶段2，integration 线）
负责人：dsh-lead（本会话认领；按"按优先级顺序认领"接 P0/P1 READY 卡）
审阅人：待定（未分配独立审阅人，不自批）
基线 commit：`35dfa152a6e378e17e83ce0251f003fa9b205a2f`（认领时 HEAD；基线 published `21899e6`）
最终 commit：`aa19558347c622d5461f20f86156754f334efdac`（tree `4e2e296eccdddcca4b32040d4efcb48fd042b451`）
认领状态：claimed → **review**

## 触发场景与行为变化

- 触发：已交付的聚合证据查询、授权导出与删除执行器**只有库内能力**，缺少受鉴权的 HTTP 消费端；用户/管理端无法查询任务证据、导出下载、执行隐私删除。
- 修改前：`observability/repository.py`（四类仓储聚合/对账）、`observability/export.py`（隐私裁剪导出、删除计划、分层执行）已交付（16-P/16-X）；无 `api/evidence_api.py`，无查询/导出/删除端点。
- 修改后（新增 [api/evidence_api.py](evoagent/api/evidence_api.py)，全部复用既有实现）：
  1. `GET /evidence/tasks/{task_id}`（admin）：真实聚合四类仓储证据，返回 summary（按隐私/信任/类型/时间）、事件**引用**（不含正文）与删除残留对账；
  2. `POST /evidence/export`（operator/admin）：按 `ExportPolicy` 隐私裁剪（operator 上限 internal；admin 可显式放宽），复用 `export_to_file` 写入 `data_dir/exports/`（根约束+排他创建），文件哈希与事件摘要持久化到审计，返回 `sha256/events_digest/audit_id`；
  3. `GET /evidence/exports/{id}/download`（admin）：导出 ID 白名单校验 + 路径根校验 + 读取后计算 sha256，与审计 detail 的 `sha256/events_digest/actor` **双重匹配**才返回受控 base64 字节；不匹配 409 拒绝（不返回假成功）；
  4. `POST /evidence/deletion`（admin）：`plan_deletion` → dry-run 返回计划（digest/引用）与执行前对账；执行时校验传入 `plan_digest`（不符 409），调用 `execute_deletion` 经 `LayerManager` 分层删除，返回逐层实际计数与执行后 `deletion_reconciliation` 残留；`fully_deleted` 恒 false，`retained_layers` 如实列出（审计层/执行器未覆盖层），明确**不宣称跨备份或全域擦除**；
  5. 主体统一来自 `ctx.auth.principal_id`；`body.principal` 与鉴权主体不一致 → 403（不能从 body 伪造 principal）。
- 顺带修复：`repository.deletion_reconciliation` 在无 `row_factory` 的 EventBus 连接上计数抛错被吞为 None 的问题，改用 `_rows()` 归一化（16-P 对账在 App 装配下才真实）。

## 范围

- 独占模块：`evoagent/api/evidence_api.py`（新增）、`tests/test_evidence_api.py`（新增）。
- 共享文件补丁（精确修改点）：`evoagent/api/server.py` **仅加一行 import**；`evoagent/observability/repository.py` 仅修复 `deletion_reconciliation` 计数归一化。
- 未改：`observability/export.py`（导出/删除实现复用）、`api/routes.py`、App/客户端。
- 兼容：纯新增端点与只读修复；无迁移、无 schema 变更。

## 验证证据

- 专项：`python -m pytest tests/test_evidence_api.py -q` → **5 passed**：真实证据聚合、导出→审计→下载字节 sha256/摘要匹配、删除 dry-run/执行/残留对账、跨主体与越权/缺 reason/未知 task/摘要不一致拒绝。
- 相关域回归：观测（repository/evidence/observability）+ api + release_evidence + 本卡 → **93 passed（21.42s）**。
- 实测证据：`.planning/2026-10-26-aln16q/api-evidence.json`，**11 项检查全过**：聚合 200（operator 403）、导出（sha256 `6a8132e7…`）、下载字节与审计摘要匹配、principal 伪造 403、dry-run、bad digest 409、执行计数（runtime_event=1 等）、删除后 runtime 残留 0、`fully_deleted=false` 且审计层在 retained_layers。
- 全量后端：`python -m pytest tests/ -q` → **2362 passed / 0 failed / 2 skipped（384.12s）**，日志 `.planning/2026-10-26-aln16q/backend-tests.txt`。
- 边界：
  - 下载为**受控 JSON(base64)** 而非流式/断点下载（文件一次读入内存；大导出需后续分块）；
  - 删除执行器实际覆盖 `runtime_events/execution_trace/diagnostic_evidence/diagnostics`；`task_side_effects`、`approvals` 与审计层由既有执行器保留（已在响应 `retained_layers`/对账中如实返回，不宣称全域擦除）；
  - 无 App/管理端 UI（属 16-E/30-B 线）；未做真机/生产证据。

## 回滚与未完成事项

- 回滚：`git revert aa19558347c622d5461f20f86156754f334efdac`（删新增端点/测试，撤销 server.py 一行 import 与 repository 计数修复；无 schema/迁移）。
- 保留缺口：
  1. 大导出分块/流式下载与导出文件保留期清理未做；
  2. 删除执行器未覆盖副作用/审批/审计层（按 16-X 语义保留，需后续政策决定）；
  3. 管理端 UI 与端侧入口未接；
  4. 真机/生产证据未采。
- 影响：G-10 的聚合观测与受控导出/删除 API 消费端交付；整包 ALN-16 仍按父包跟踪。

## 审阅结论：待审

## 整包结论：仍实现中（API 层交付；流式/保留期/管理端 UI 与生产证据待补）
