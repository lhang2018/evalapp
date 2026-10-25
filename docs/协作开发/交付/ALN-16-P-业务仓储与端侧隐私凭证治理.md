# ALN-16-P 交付：业务仓储与端侧隐私凭证治理

任务：ALN-16-P 业务仓储与端侧隐私凭证治理
负责人：zcode-next
审阅人：待定（须独立复核，不接受自审自收）
基线 commit：`89accb6`（认领锚点）；分支 feature/user-direction，交付 commit 见 git log "feat(ALN-16-P)"
认领状态：delivered（review，待独立复核）

## 触发场景与行为变化

- 输入/触发：16-A 交付的 EvidenceEvent 契约与 16-X 的导出/删除执行
  此前**零业务消费者**——运行事件/审计/副作用账本/审批四类业务数据
  不在证据链上，导出与删除计划无法消费真实数据；端侧事件与服务端
  契约无对齐形态。
- 修改后：新增业务仓储装配层——四表行投影为 EvidenceEvent 并聚合，
  导出/决策链/删除计划首次接真实数据；端侧新增契约对齐投影；
  删除/保留/残留可对账。

## 范围

- 服务端独占新增：
  - `evoagent/observability/repository.py`：四类投影
    （project_runtime_event / project_audit_row / project_side_effect_row /
    project_approval_row）、`collect_for_task` 聚合（支持双库部署：
    EventBus 独立 events.db + 主库，兼容单库）、
    `deletion_reconciliation` 残留核对；
  - `tests/test_observability_repository.py`（12 项）。
- 端侧独占新增：
  - `android-app/src/offline/evidence.ts`：OfflineEvent → 契约对齐
    ClientEvidenceEvent 投影（origin=device、四级→契约级、故障推断、
    敏感脱敏、密钥拒绝）；只做投影不改上传链路（privacy 过滤原样）；
  - `android-app/test/offline/evidence.test.ts`（5 项）。
- 未动：server.py（并行在途）——装配查询端点注册留缺口；上传链路
  改造（端侧事件以 EvidenceEvent 形态上报）属后续卡。

## 关键语义（验收对照）

1. **受信身份隔离**：服务端表投影 origin=server/trust=trusted；
   untrusted 行必须携带 untrusted_origin，缺失即拒绝（不因投影放宽）；
   端侧 origin=device/trust=trusted。
2. **最小证据与脱敏**：设备动作/副作用/审批（动作摘要与目标敏感）
   投影 restricted+redacted——正文不复制只留 payload_ref；
   未知隐私分级 fail-safe 到 restricted（同样强制脱敏）；
   全链过 assert_context_safe/containsSecret（疑似密钥直接拒绝）。
3. **删除/保留/失败可对账**：装配 → plan_deletion（digest 锁定）→
   execute_deletion（16-X 既有，幂等+墓碑）→ deletion_reconciliation
   逐表核对残留——全零才报 fully_deleted，任何残留显式列出，
   不返回假成功；删除后装配为空（证据不再可检索）。

## 实现要点（审阅关注）

- EventBus 双库形态：App 的 runtime_events 落独立 events.db（原生
  sqlite3 无 row_factory），装配层按 cursor 列名映射兼容两种连接；
- bus.emit 未携带 refs JSON 的历史行走行内 task_id/run_id 列回落；
- 客户端 privacy 四级（standard/sensitive/secret/private）→ 契约四级
  映射：standard→internal，sensitive→sensitive，secret/private/未知
  →restricted（fail-safe）。

## 验证证据

- 服务端专项：`python -m pytest tests/test_observability_repository.py -q`
  → **12 passed**（trusted 投影/敏感脱敏/untrusted 拒绝/密钥拒绝/
  fail-safe/最小证据/refs；聚合五源时序/范围强制/计划覆盖/执行后
  干净对账/删除后不可检索/残留诚实上报）。
- 观测回归：test_observability + test_observability_evidence +
  test_export_deletion_exec + test_peer_trace_export_review →
  **65 passed**。
- 全量：`python -m pytest tests -q --ignore=tests/test_goal_budget_migration.py`
  → **1848 passed / 1 skipped / 0 failed**（排除文件为并行 15-P 在途
  半成品；上轮的 ledger_drift 失败已被并行会话修复，本轮消失）。
- 客户端：`npm run test:offline` → **285 passed / 0 failed**（+5 项）；
  `npm run typecheck` 零错误。
- 边界：全部本地自动化（临时目录双库 + 内存投影）；生产身份/存储
  范围联测（真实多主体导出审批、生产存储分层）按任务卡环境另验。

## 回滚与未完成事项

- 回滚：revert 单提交；纯新增模块，无迁移。
- 未完成（按任务卡原范围保留）：
  1. 装配查询 API 端点（api/observability_api.py 注册需 server.py
     空闲后一行 import，串行让位）；
  2. 端侧 EvidenceEvent 上报链路（投影已备，接线属后续卡）；
  3. 业务缓存层（如模型响应缓存）的证据接入未覆盖；
  4. 生产身份与存储范围联测。
- 影响主题：E-01（统一事件）、S-02（隐私分级与脱敏）、S-05（审计
  证据链）、M-10/O-03（删除与导出边界）、UX-17/UX-24/G2。

## 审阅结论

待审。建议重点：restricted 判定的事件类型集合是否完备（设备观测/
记忆写入/位置等在端侧集合、服务端按 event_type 前缀）；双库形态
deletion_reconciliation 与 LayerManager（16-X）的职责边界；密钥
检测模式的召回边界（正则仅覆盖常见形态，非完备）。
整包结论：仍实现中。
