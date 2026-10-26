# ALN-27-C 知识持久化真实注入与迁移质量验证

> 2026-10-26 知识消费接续复核：跨连接知识鲜度/撤销、写失败和宿主事务、整批迁移回滚、不可信/评测注入控制、压缩后计数已修。正式验收项2用户/项目身份绑定仍未实现；不能以适用域过滤代替身份隔离。 修复a0de595/36d078a，固定相关341过；新增修复待另一审阅人复核。下文为原交付历史记录，最新行为与正式接受缺口见[复核报告](../验收/2026-10-26-知识持久化与上下文注入复核.md)，不扩大接受。

任务：ALN-27-C / 知识持久化真实注入与迁移质量验证（P1，阶段4，assets 线；ZG-09）
负责人：dsh-lead（30-A 一审接受解锁后认领）
审阅人：待定（未分配独立审阅人，不自批）
基线 commit：`39843d62143cb909367007b27876f6d3b83e314b`（认领时 HEAD；基线 published `21899e6`）
最终 commit：`79cc06b9838e30ab0c693428d7a3bcc5aaa9c0aa`（tree `36ba800519f027090386d58188af08ca5bba816c`）
认领状态：claimed → **review**

## 触发场景与行为变化

- 触发：27-A/30-A 的知识库是**进程内存储**（重启即丢、版本/撤销无持久化），`plan_injection` 只存在于库层、未接真实上下文装配；迁移收益只有内存口径，缺"真实前后任务数据"消费。
- 修改前：`KnowledgeStore()` 纯内存；`ContextAssembler` 不消费知识；`app.knowledge` 进程内；`transfer.evaluate_transfer` 只接受手工构造的 `TaskOutcome`。
- 修改后（复用既有切片，不新建知识权威）：
  1. [knowledge/store.py](evoagent/knowledge/store.py)：
     - `KnowledgeStore(conn)` 增加 SQLite 持久化（`knowledge_entries` 幂等建表，`add/revoke` 写穿；重启保留**版本链、撤销时点/原因、来源**；`digest()` 全库摘要）；显式 `KnowledgeStore()` 保持旧内存用法兼容；
     - 新增 `migrate_knowledge(source, target)`：actor/reason 必填；目标非空且不同→拒绝覆盖；逐条迁移后 **digest 核验**；撤销状态保真；重复迁移幂等 `already_migrated`；源=目标拒绝。
  2. [memory/context.py](evoagent/memory/context.py)：`ContextAssembler` 增加 `knowledge_store` / `knowledge_min_confidence`；装配时经 `plan_injection` 按**适用域+可信度**注入，**撤销/低可信/不适用条目不进入模型消息**；manifest 新增 `kind="knowledge"` 条目记录 `source/source_id/source_version` 与是否 included。
  3. [knowledge/transfer.py](evoagent/knowledge/transfer.py)：`outcomes_from_tasks` 把**真实任务记录**转 `TaskOutcome`（通过与否取任务自身状态），迁移收益仍只按任务质量判定（命中率不能替代）。
  4. [app.py](evoagent/app.py)：`knowledge` 改为**持久库**（`self.db._conn`）并注入 `assembler`；旧 `KnowledgeStore()` 调用兼容。

## 范围

- 独占/边界内模块：`knowledge/store.py`、`knowledge/transfer.py`、`memory/context.py`、`app.py`（卡片写入边界）；新增 `tests/test_knowledge_persistence.py`。
- 未改：`applicability.py`（100% 复用 `plan_injection`）、`KnowledgeEntry` 校验语义（来源/可信度/适用域/版本）、发布基线。
- 兼容/迁移：SQLite 幂等建表，无版本化迁移；`conn=None` 行为与 27-A 完全一致；旧内存条目可经 `migrate_knowledge` 受控迁移并核验。

## 验证证据

- 专项：`python -m pytest tests/test_knowledge_persistence.py tests/test_knowledge.py -q` → **28 passed**（新增 7 项 + 既有 21 项）：
  - 重启保留版本链/撤销时点/来源；内存旧用法兼容；
  - 内存→持久迁移：digest 一致、撤销保真、幂等、冲突/缺 actor/reason/同库拒绝；
  - 上下文注入：适用+高可信进入消息与 manifest（source/version 可追溯）；撤销、低可信（<0.5）、跨域条目不进入；
  - 真实任务记录 → `TaskOutcome` → 迁移前后质量 improved/degraded；仅 hit_rate → unproven；
  - App 持久库写入→重启仍在，`assembler.knowledge is app.knowledge`。
- 相关域回归：记忆/上下文/知识（`test_memory*.py`、`test_context*.py`、`test_knowledge*.py`）→ **103 passed（5.71s）**。
- 实测证据：`.planning/2026-10-26-aln27c/knowledge-persistence-evidence.json`，**12 项检查全过**（真实 SQLite/App/真实任务记录/真实 ContextAssembler）。
- 全量后端：首跑出现 17 failed——其中 **16 项位于 `tests/test_benchmark_review_20261026.py`**（其他会话**未提交**的 `evoagent/evaluation/host_bench.py` 在途改动 + 该未跟踪复核测试文件，非本次改动，我未触碰）；另 1 项为 30-A 旧断言 `knowledge_store_is_not_shared_across_instances`，其语义已被 27-C 持久化取代，已按新语义更新为 `test_knowledge_store_persists_across_instances`（实例对象仍独立、内容持久化）。排除他人未跟踪 WIP 复核文件后复跑：**2433 passed / 0 failed / 1 skipped（402.12s）**，日志 `.planning/2026-10-26-aln27c/backend-tests-clean.txt`。随后他人在途修复已提交（`fde8f89`），最终 HEAD 全量复跑：**2462 passed / 0 failed / 1 skipped（412.82s）**，日志 `.planning/2026-10-26-aln27c/backend-tests-final.txt`（含他人已提交的基准复核修复，非本卡范围）。
- 边界（如实登记）：
  - 未接真实 LLM，上下文注入为真实装配器但模型为后续评测链；
  - 迁移仅覆盖**内存→持久**单场景；跨实例/批量历史迁移未做；
  - "相同固定任务集"在实测中用相同任务 ID 口径（单测），未跑真实 LLM 长任务集前后对照；
  - 多进程并发写知识库未压测（依赖底层 SQLite/锁语义）。

## 回滚与未完成事项

- 回滚：`git revert 79cc06b9838e30ab0c693428d7a3bcc5aaa9c0aa`（恢复内存 `KnowledgeStore` 与旧 `ContextAssembler`；新增表为幂等建表，回滚不影响旧路径）。
- 保留缺口：
  1. 真实 LLM 端到端"知识进入模型消息并改变任务结果"未做；
  2. 跨实例/批量迁移与迁移回滚（持久→内存/备份恢复）未做；
  3. 多进程并发写与知识库损坏恢复未压测；
  4. 客户端/CLI 知识管理入口仍按 27-A 保留。
- 影响：ZG-09 的持久化与注入/迁移验证交付；知识整包按父包跟踪，不冒称关闭。

## 审阅结论：待审

## 整包结论：仍实现中（真实 LLM/批量迁移/并发与恢复待补）
