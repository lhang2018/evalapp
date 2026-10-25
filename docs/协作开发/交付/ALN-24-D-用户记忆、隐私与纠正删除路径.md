# ALN-24-D 用户记忆、隐私与纠正删除路径

任务：ALN-24-D / 用户记忆、隐私与纠正删除路径
负责人：dsh-lead（本会话认领）
审阅人：待定（未分配独立审阅人，不自批为独立验收）
基线 commit：`c98439a414861f9f99b1f2c2116346e1556d95fa`（认领时的 `feature/user-direction` HEAD，不是已发布共同基线）
分支与最终 commit：`feature/user-direction` / `799ac22778750c28b9ac39420cb80beb05b049a3`（tree `5054ce2e3909ea17e2fc68f3655812e07e22fa85`）
认领状态：claimed → **review**

## 触发场景与行为变化

- 输入/触发：用户要「看到记忆是从哪来的、把记错的事实改掉、把某条记忆删掉、调整哪些记忆能参与分析」。
- 修改前：
  - ALN-05 已接受存储/冲突/墓碑切片（`MemoryStore.correct` 会把来源置 user、状态置 verified；`delete_with_chain` 沿派生/缓存引用链写墓碑并失效冲突），但**没有用户控制面**：没有内存列表/来源展示、没有纠错后「后续检索确实用新事实」的验证、没有删除后的保留项/失败项说明、也没有「调整记忆/分析范围」的持久策略。
- 修改后：
  - 新增 `evoagent/memory/user_control.py::MemoryControlService`（只复用、不重写既有切片）：
    - **看来源**：列表/详情返回公共字段 + `provenance{source,scope,scope_key,fact_key,task_id}`，明确不含证据快照；隐私口径标注「仅本地记忆库，不导出」；
    - **纠错**：调 `MemoryStore.correct` 后**回检检索**——后续检索必须召回新内容且不再召回旧内容（GLOBAL-02「后续使用纠正事实」），把 `verified_in_retrieval` 写进结果；
    - **删除**：调 `delete_with_chain`（墓碑 + 冲突失效），返回 `deleted_ids/tombstones/invalidated_conflicts`、`recall_check`（删除后取不到且检索不再召回），并**逐层列出保留项**（墓碑仅摘要、审计日志、历史备份未追删）与 `failures`；`fully_erased` 恒为 `False`，`scope_claim` 明确「仅本地记忆库与检索缓存，不宣称跨备份/跨端/外部仓储擦除」；
    - **范围调整**：`memory_scope_policy`（记忆库内幂等建表，不动共享 schema）保存允许/禁止作用域，**禁止优先**；未知作用域、allow/deny 重叠、空集合、空理由均拒绝。
  - `evoagent/api/memory_context_api.py` 追加 6 个注册表端点：`GET /memory/items`、`GET /memory/items/{id}`、`POST /memory/items/{id}/correct`、`POST /memory/items/{id}/delete`、`GET /memory/scope-policy`、`PUT /memory/scope-policy`（由 `server.py` 既有 import 自动登记，不改其 if 链）。

## 范围

- 独占模块：`evoagent/memory/user_control.py`（新增）。
- 共享文件补丁（精确修改点）：`evoagent/api/memory_context_api.py`——只在文件末尾追加用户记忆控制端点与 3 个 import；既有 `context-manifests` 端点一行未改。**未改** `memory/store.py`、`memory/conflicts.py`、`memory/context.py`、`server.py`、`App.tsx`/客户端。
- 新增/修改契约：新增 `memory_scope_policy`（记忆库内表）、6 个 HTTP 端点与响应结构（provenance/retained/recall_check/fully_erased/scope_policy）；既有记忆检索与墓碑语义不变。
- 数据迁移/兼容：不新增版本化迁移；策略表为幂等 `CREATE TABLE IF NOT EXISTS`，旧记忆库首次访问时自动建表。

## 验证证据

- 正常路径：
  - 列表/详情返回来源且不含 `evidence`（证据快照不外露）；`privacy.storage=local-memory-db`；
  - 纠错后：`source=user`、`status=verified`、`verified_in_retrieval=True`，检索「上海」命中纠正后内容、检索「杭州」不再命中该条；
  - 删除后：`deleted_ids` 含源记忆与派生链、墓碑写入、`get` 返回 None、检索不再召回；`retained` 三层齐全、`fully_erased=False`；
  - 范围策略：`deny` 优先于 `allow`（allow=[user,project]、deny=[shared] 时 `shared=False`），策略读回一致。
- 拒绝/失败/越权：
  - 未知记忆详情/纠错/删除 → 404；空内容、空理由、空 actor → `MemoryControlError`；非法作用域、allow∩deny、空 allow+deny → 拒绝；
  - API：无令牌 → 401；operator 调用 `/memory/*` → **403**（沿用既有「全局记忆管理需要 admin 权限」门禁）；admin 可读/纠错/删除/改策略；`PUT /memory/scope-policy` 非 admin → 403。
- 命令、结果、日志与对应源码 commit：
  - 提交 `799ac22778750c28b9ac39420cb80beb05b049a3`；
  - 本卡专项：`python -m pytest tests/test_user_memory_control.py -q` → **9 passed**；
  - 记忆/接口相关域回归（memory/conflicts/manifest/scope/owner_partition/evaluation/runtime_memory_identity/api + 本卡）→ **92 passed（22.98s）**；
  - 全量后端：`python -m pytest tests/ -q` → **1974 passed / 2 failed / 1 skipped（336.27s）**，日志 `.planning/2026-10-25-aln24d/backend-tests.txt`。
  - 2 项失败**与本卡无关**：均在 `tests/test_peer_action_binding_review.py`（ALN-07-C 系），失败断言缺 `step_binding_error`，同时工作区存在他人**未提交**的 `evoagent/planning/binding.py` 改动；本卡未改绑定/Planning/ReAct 任何文件。
- Mock / 本地组件 / 真机 / 真实供应商的验证边界：
  - 本轮为本地 SQLite 记忆库 + 真实 HTTP API 进程，不是生产多用户身份体系；
  - 当前部署为单用户本地实例：`/memory/*` 沿用既有 admin 门禁，**operator（普通用户）视角需 24-C 会话身份接线后放开**（保留缺口）；
  - 删除只覆盖本地记忆库与检索路径；历史备份/跨端/外部仓储未追删，报告明确不宣称全域擦除；
  - 未接 App 端记忆管理界面（客户端接线属 24-D/24-C 后续，`App.tsx` 未改）。

## 回滚与未完成事项

- 回滚触发、操作及验证：`git revert 799ac22778750c28b9ac39420cb80beb05b049a3`（删新服务与测试、撤掉 API 追加端点；`memory_scope_policy` 表可保留，幂等建表不影响旧代码）。回滚后 `tests/test_user_memory_control.py` 整体消失，记忆/墓碑既有行为不变。
- 保留的限制、后续 ALN 和依赖：
  - operator/普通用户视角需 24-C 会话身份接线（当前仅 admin token 可访问 `/memory/*`）；
  - App 端「记忆管理」界面与端侧缓存联动未接（客户端接线）；
  - 删除未追删历史备份与跨端副本（明确列为保留项）；如需「彻底擦除」需要独立的备份清理与外部仓储任务；
  - 范围策略目前只作用于控制面过滤，尚未接入 ContextAssembler 的检索过滤（装配器接线属 05/24-C 范围，报告中已注明）；
  - 未做多用户并发/大规模记忆库性能验证。
- 影响的主题/实体/ADR/契约/系统验收条目：新增用户记忆控制响应契约与 `memory_scope_policy`；既有存储/冲突/墓碑契约不变；UX-17/UX-24 的服务端部分交付，端侧界面待接线。

## 审阅结论：待审

## 整包结论：仍实现中（ALN-24：端侧界面、会话身份与彻底擦除范围仍待；本卡交付用户记忆控制面）
