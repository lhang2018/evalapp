# ALN-16-A 跨端观测契约、决策链、隐私与凭证治理

## 行为变化

新增跨端统一证据契约 `EvidenceEvent`：固定四个隐私等级（public/internal/sensitive/restricted）、四个故障等级（none/transient/degraded/fatal）、来源（device/server）与信任标记（trusted/untrusted），并用 `EventRefs` 统一挂 task/run/node_run/effect/approval/diagnostic/candidate/release/trace/audit 引用。校验拒绝非法枚举、untrusted 无来源、restricted 内联 payload 未脱敏以及形似凭证的 payload；同一事件可计算 canonical digest，用于跨端去重与核对。

既有 `RunEvent` 增量支持 `origin`、`trust`、`fault_level`、`untrusted_origin`、`refs`、`redacted` 六个字段并落库；旧行读取时补默认值（server/trusted/none/空 refs），既有事件写入与同步路径不变。`EvidenceEvent.from_run_event()` 把原有 `classification` 映射到四等级，并按事件类型/严重度推断故障等级，避免端侧另造一套标签。

`build_decision_chain()` 可按 task/run 重建决策链：把事件与 approval/effect/diagnostic/evaluation/release 记录统一成节点，按 sequence/timestamp/id 稳定排序，生成 `caused_by`、`approved_by`、`released_in`、`evaluates`、`promoted_to` 边，并列出缺失引用（候选、审批、诊断、发布、效果、node_run、事件）。链支持按 effect/approval/node_run/release 索引，`digest()` 在相同输入下稳定，便于审计复现。

外部网页/文件/工具返回/诊断内容统一经 `wrap_external(content, origin)` 标记：必须带来源，产出 `trust=untrusted`、`instruction_authority=False`，杜绝外部内容被当作指令或扩权依据。导出默认只含 internal 及以下；敏感/受限需 admin 显式放宽，operator 放宽直接报错。导出必须记录 actor 与 reason，可限定 task；事件做最小化（不含 payload 正文，仅可选 `payload_ref`），保留 untrusted 来源，递归清洗疑似密钥，输出确定性 `events_digest`。删除必须先给出 task/actor/reason，产出删除计划与含范围摘要的墓碑，便于解释“删了什么、为什么”。

凭证按 service_signing/device_signing/client_trust/api_token/model_provider 六级管理，仅 client_trust 公钥可分发给客户端；密钥本体只存 SHA-256 指纹，比较用常量时间。轮换保留旧密钥至宽限期结束，撤销必须记录 actor 与 reason，状态查询可解释“为何不可用”。`TokenRing` 支持 API token 多代轮换与角色解析，`ApiAuth` 接入后旧 admin/operator token 仍兼容。`assert_context_safe` 阻止敏感键名与凭证样式内容进入模型上下文、导出与日志。

## 接口与兼容

- 新增包 `evoagent/observability/`：`contract.py`（`EvidenceEvent`、`EventRefs`、`SCHEMA_VERSION`、`to_four_level`、`infer_fault_level`、`canonical_digest`）、`chain.py`（`DecisionChain`、`build_decision_chain`）、`export.py`（`wrap_external`、`ExportPolicy`、`EvidenceExporter`、`plan_deletion`）、`credentials.py`（`CredentialRegistry`、`TokenRing`、`scrub_*`、`assert_context_safe`）。
- `evoagent/core/events.py`：`RunEvent` 新增可选字段；SQLite 表新增列并兼容旧行，`_EVENT_COLUMNS` 扩至 25。
- `evoagent/core/privacy.py`：新增 `SPEC_PRIVACY_LEVELS`/`SPEC_EXPORTABLE_LEVELS`/`to_spec_level`/`is_spec_exportable`，保留原分类函数。
- `evoagent/api/auth.py`：`ApiAuth` 可注入 `TokenRing`；无 ring 时行为与旧 token 校验一致。
- 所有新字段均为可选/有默认值，未接入的调用方无需迁移；未改动共享台账。

## 验证

- 新增 `tests/test_observability_evidence.py` **11 passed**：旧事件映射与非法等级/缺来源/受限未脱敏/凭证样式拒绝、RunEvent 新字段落库回读、决策链引用与缺失引用、效果/因果未决、链 digest 与索引稳定、外部来源保留与清洗、导出角色最小化/正文排除/确定性、密钥清洗、删除计划、凭证分级/轮换/撤销/状态解释、上下文泄漏拦截、TokenRing 轮换与 ApiAuth 角色。
- 联合专项（含既有事件契约、事件同步、控制面）**41 passed**，日志 `.planning/2026-10-22-aln-11-16/aln16-targeted.txt`。
- 服务端全量 `python -m pytest -q` **1076 passed / 1 skipped**，日志 `.planning/2026-10-22-aln-11-16/python-full.txt`。
- 源码提交：`bc7eb65050ca5a704918e739cb9596ca4caf8b30`。

## 回滚与未完成事项

1. 回滚触发：事件表迁移或鉴权改动导致读写异常。回滚操作：恢复 `evoagent/observability/`、`core/events.py`、`core/privacy.py`、`api/auth.py` 至上一提交；新列有默认值，旧代码读取新库不受影响（仅忽略新列）。
2. 端侧尚未产出 `EvidenceEvent`（Android 事件仍走原 RunEvent/离线队列），跨端字段对齐需在后续接线中完成。
3. 决策链目前接受调用方传入 approval/effect/diagnostic/evaluation/release 记录，尚未与各业务仓储自动装配；真实任务/发布的端到端接入未完成。
4. 导出/删除只产出计划与墓碑，尚未真正执行存储删除或对外导出下载；生产鉴权的密钥保管方式（HSM/KMS）与签名密钥分层落地仍待部署侧实现。
5. 未认领/未改动并行会话文件与共享台账。

审阅结论：待审（owner `claude-main`，reviewer `codex-runtime`，不得自审自收）。


## 独立验收补记（2026-10-22）

原交付的验证计数与真机证据保留为历史范围。本轮独立审阅发现边界缺陷，已提交 `aae0f9b` 修复，新增29项联合回归；隔离源码全量后端1095 passed/1 skipped、提交内客户端165 passed、typecheck通过，修复后未重跑真机。详见[独立验收报告](../验收/2026-10-22-ALN11-16独立验收.md)。本轮修复由 codex-runtime 编写，待其他 reviewer 复核；原卡仍为 review，不关闭整包，不发布基线。
