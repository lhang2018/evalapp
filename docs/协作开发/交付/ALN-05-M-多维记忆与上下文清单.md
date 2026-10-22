# ALN-05-M 多维记忆、ContextManifest 与冲突/删除

## 行为变化

记忆读写与上下文装配现在带五类身份作用域：user/project/session/task/expert。写入时可传 `MemoryIdentity`，身份键钉在条目的 extra 字段上（不新增表）；检索默认只返回当前身份链上键匹配的条目，未声明归属的条目不会泄漏。expert 是横向蜂巢，只有显式 `expert_id` 才可见。

跨作用域读取必须使用带 `actor`/`reason` 的 `AccessGrant`；对具体条目的越权 `get` 与对未授权作用域/键的显式 `search` 直接抛 `MemoryAccessError`，不是静默空结果。

同一 `fact_key` 的新旧值不会互相覆盖：新内容写入后生成未裁决冲突记录，双方条目都带 `conflict_ids` 与 `has_conflict`。规则裁决（显式 user 偏好优先，其次可信度排序）与人工裁决是两个入口，人工裁决强制记录 actor 与 reason，胜者保留、败者置 revoked 并写 `superseded_by`；规则无法裁决时保持 unresolved 并注明需人工。

删除源记忆改为写墓碑并沿 `derived_from` / `cached_from` 引用链 BFS 删除派生摘要与缓存；相关未裁决冲突标记 invalidated，幸存条目的冲突标记同步清除。墓碑记录内容摘要、身份 JSON、根条目、actor 与 reason，可按根条目审计。

每次上下文装配生成不可变 `ContextManifest`：条目 ID、来源、原始记忆 ID、来源版本/时间、被选原因、token 估算、内容摘要、冲突标记、压缩前后引用与装配参数摘要。清单只含引用与摘要，不含记忆正文；同一输入重复装配得到相同 digest，与装配时间无关。压缩继续保留硬约束与验收条件，并把被压掉的条目以 `included=False` 留在清单中。

## 接口与兼容

- 新增 `evoagent/memory/manifest.py`：`MemoryIdentity`、`AccessGrant`、`MemoryAccessError`、`ContextEntry`、`make_entry`、`ContextManifest`。
- 新增 `evoagent/memory/conflicts.py`：`ConflictLedger`、`TombstoneLedger`。
- `MemoryStore`：锁改为 `RLock`（账本与存储共用临界区）；新增 `Scope.USER/SESSION`；`write(..., identity=, fact_key=)`；`search/get/list_items(..., identity=, grant=)`；`delete_with_chain`、`list_conflicts`、`adjudicate_conflict`、`resolve_conflict`；`delete` 保持布尔返回值但改为墓碑级联。
- `ContextAssembler.assemble(..., identity=, grant=)` 生成并挂载 `ctx.manifest`；`stats()` 增加 `manifest_id`/`manifest_digest`。
- 未传 identity/grant 的既有调用路径行为保持不变；数据库仅使用既有 `extra` JSON 增量字段，无新表/迁移。`Scope.USER/SESSION` 为新增枚举值。

## 验证

- 新增 `tests/test_memory_manifest.py` 与 `tests/test_memory_conflicts.py` 共 **15 passed**：四类身份互不串读、未声明归属不泄漏、专家蜂巢显式 ID、跨作用域授权/拒绝与审计字段、list_items 隔离、清单 digest 可复现、正文不入清单、冲突标记可见、压缩保留约束/验收并标记丢弃引用、删除后清单仍保留引用、同事实键不覆盖、显式偏好/高可信/平局三条裁决路径、人工裁决审计、墓碑级联与冲突失效。
- 发布与记忆联合专项 **151 passed**，日志 `.planning/2026-10-22-aln-03-05/targeted.txt`。
- 全量 `python -m pytest -q`：首轮 **1051 passed / 1 skipped**，2 项共享台账校验失败来自 `IMPLEMENTATION.md` 的 M-07 陈旧声明与行号引用位移（本卡未改共享台账），另 1 项异步审批用例偶发 `SUSPENDED`；复跑 **1055 passed / 1 skipped**，审批用例通过，仅余同样 2 项台账失败（并行会话新增用例使总数上升）。日志 `.planning/2026-10-22-aln-03-05/full.txt`、`full-rerun.txt`。
- 源码提交：`53fc7bb940168b59364888634a296ecf39f4c051`。

## 回滚与未完成事项

1. 回滚触发：身份过滤导致既有调用方大面积读不到记忆。回滚操作：恢复 `evoagent/memory/` 至上一提交；未传 identity 的路径不依赖新表，可直接回退。
2. `core/task.py` 的身份字段、共享持久化/schema 迁移与 controller 装配仍由集成角色合入；本卡只提供 store/context 入口。
3. 端侧离线记忆与缓存失效（ALN-12）、隐私分级与审计保留期（ALN-16）未关闭。
4. 共享台账需由对应会话更新：`IMPLEMENTATION.md` M-07 仍写 `ContextManifest` 零命中，`IMPLEMENTATION.md`/`FEATURES.md`/`GAP_ANALYSIS.md` 的裸行号引用因 `store.py`/`context.py` 行号位移需重新锚定。

审阅结论：待审（owner `claude-main`，reviewer `codex-runtime`，不得自审自收）。
