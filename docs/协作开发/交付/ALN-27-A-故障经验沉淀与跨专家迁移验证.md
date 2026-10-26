# ALN-27-A 交付：故障经验沉淀与跨专家迁移验证

> 2026-10-26 知识消费接续复核：公共知识验证、NaN门禁和固定任务比较缺陷已修，原接受重新review；重复/非布尔样本拒绝、不同任务清单unproven。真实运行版本/数据集绑定和LLM质量仍待。 修复a0de595/36d078a，固定相关341过；新增修复待另一审阅人复核。下文为原交付历史记录，最新行为与正式接受缺口见[复核报告](../验收/2026-10-26-知识持久化与上下文注入复核.md)，不扩大接受。

任务：ALN-27-A（PRODUCT-2026-10-25，父包 ALN-27，阶段4）
负责人：zcode-main；审阅人：待定（须独立复核）
基线 commit：`af5e66e3abba81f43e91031781b315b42f5dea9a`（接手锚点，非已发布基线）
分支与最终 commit：feature/user-direction（源码随本卡提交落库）
认领状态：delivered（review，待独立复核）

## 目标与行为变化

故障经验（知识）必须**可追溯**：每条经验带来源、可信度、适用域与版本；错误经验
可撤销；注入前做适用性检查；跨专家迁移收益用**任务质量前后对照**证明，不接受命中率。
本卡新增独占模块 `evoagent/knowledge/`，不重新实现已接受切片，不改 runtime 既有文件。

## 范围

- 独占新增：`evoagent/knowledge/`（`store.py` / `applicability.py` / `transfer.py` / `__init__.py`）。
- 测试：`tests/test_knowledge.py`（21 项）。
- 共享文件：无（本卡不触碰 `evoagent/memory/`、`evoagent/evolution/` 既有文件，仅新增包）。
- 不新增表、不改契约、不改 controller/api；未接入运行时注入（见保留缺口）。

## 验收对齐

1. **知识有来源/可信度/适用域/版本与撤销**：`KnowledgeEntry.validate` 强制来源非空、
   可信度 ∈[0,1]、适用域非空、版本 ≥1；`KnowledgeStore.add` 要求同 `key` 版本严格递增、
   `entry_id` 不重复；`revoke` 幂等且不覆盖首次撤销时点。
2. **不适用条目不注入**：`plan_injection` 对已撤销/适用域不符/可信度低于阈值一律跳过并
   记录原因；可注入条目按可信度→版本降序。
3. **错误经验可撤销**：撤销后的条目从注入计划中消失（带原因），`latest(key)` 回退到
   仍有效的旧版本（等同回归回滚），被撤销的那条永不注入。
4. **跨专家前后对照证明收益，不用命中率代替任务质量**：`evaluate_transfer` 以任务
   通过率（任务质量）计算每专家与总体前后差；只给命中率而无真实任务结果 → `unproven`；
   前后专家集合不一致 → `unproven` 不归因；退化专家在 `degradations` 中显式列出。

## 验证证据

- 新增专项：`tests/test_knowledge.py` **21 passed**。
- 后端全量（显式带 `tests/`）：**1889 passed / 1 failed / 1 skipped**，耗时 430s。
  - 唯一失败 `tests/test_attachments.py::TestAttachmentApi::test_chunked_upload_complete_and_content`
    （附件 `X-Attachment-SHA256` 响应头）与本卡无关：本卡为纯新增 `evoagent/knowledge/`，
    未被任何运行时模块导入，不触及 api/attachments。
  - 命令：`python -m pytest tests/ -q`（系统 Python 3.10.11）。
  - 日志：`.planning/2026-10-25-aln27a/backend-tests.txt`。

## 回滚与未完成事项

- 回滚：删除 `evoagent/knowledge/` 与 `tests/test_knowledge.py` 即可；无迁移、无 schema 变更。
- 保留缺口（真实消费路径）：把 `plan_injection` 接入记忆/上下文装配与专家运行时的
  **真实注入**、`evaluate_transfer` 接入演化评测的**真实前后数据集**仍待实施；本卡只提供
  可复核的门禁与验证器，不声称已在真实运行路径生效。
- 影响主题：A-69 / B-81 / B-88；父包 ALN-27 关闭另按整包依赖推进。

审阅结论：待审
整包结论：仍实现中
