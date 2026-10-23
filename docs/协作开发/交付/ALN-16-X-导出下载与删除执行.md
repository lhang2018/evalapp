# ALN-16-X 交付：导出下载与删除计划的实际执行

2026-10-24独立审核：**需修改，修复后待另一审阅者复核**。codex-runtime发现问题并提交修复`f633130`；27项独立探针原源码26失败/1通过，修复后相关155通过，固定源码1521通过/1跳过。见[独立审核记录](../验收/2026-10-24-新增三卡独立审核.md)。以下为dfcfcee原交付范围和历史证据，不代表修复后的接受结论。

任务：ALN-16 / ALN-16-X 导出下载与存储删除实际执行（16-A/16-R 后遗留接线）
负责人：claude-main
审阅人：待定（须作者之外独立复核）
基线：未发布基线；共享候选工作区增量开发
分支与最终 commit：main，代码 `dfcfcee`
认领状态：review（待独立复核）

## 触发场景与行为变化

- 修改前：`plan_deletion` 只产出删除计划（event_ids+digest+墓碑摘要），
  没有执行器——「实际删除」欠账；导出报告只返回 dict，无落盘产物与文件审计。
- 修改后（`evoagent/observability/export.py` 追加 99 行，纯加法）：
  - `execute_deletion(deleter, plan, *, actor, reason, verify_digest=True)`：
    执行前重算 `canonical_digest(event_ids)` 与计划比对，不一致即
    `DeletionScopeError`（fail-closed，不删任何数据）；actor/reason 缺失、
    scope 为空拒绝；调 `deleter.delete_task(task_id)` 逐层删除；
    幂等——同计划重放第二次计数全 0 且 ok=True；墓碑补 executed_* 审计字段；
  - `export_to_file(exporter, events, path, *, actor, reason, task_id,
    allowed_root, now)`：导出报告写 JSON 文件（sort_keys 确定性），
    返回文件 sha256 + 审计块（actor/reason/task_id/events_digest/count）；
    路径经 realpath 解析后必须落在 `allowed_root` 内——`..` 与 symlink
    逃逸均 `ExportDenied`。

## 范围

- 共享文件：`evoagent/observability/export.py`（追加，不改既有函数）；
  workboard 已注册该文件为共享（stream=models）。
- 独占新增：`tests/test_export_deletion_exec.py`（10 项）。
- 不修改：core/events.py 表结构、log_layers.py、server.py、Android。
  `delete_task` 原语已存在（`core/log_layers.py`），本卡只约定 duck-type 接口。
- 未宣称：API 端点暴露、端侧 EvidenceEvent 接线（归其它卡）。

## 原交付验证证据（历史）

- `python -m pytest tests/test_export_deletion_exec.py -q` → **10 passed**
- 组合回归（含既有 observability 41 项）→ 97 passed
- 判别力：`test_digest_mismatch_rejected`（篡改 event_ids 后 digest 校验拒绝且
  `deleter.calls==0` 未删数据）、`test_export_symlink_escape_rejected`（symlink
  逃逸拒绝）、`test_idempotent_replay`（二次执行 total=0 且 ok）。
- 边界：内存版逐层删除器 + tmp_path 真实文件；未跑真实 SQLite 仓储全链
  （`delete_task` 原语本身由既有测试覆盖）。

## 回滚与未完成

- 回滚：还原 export.py 追加段即可（纯追加，无签名变更）。
- 未完成：API 层暴露删除/下载端点、端侧 EvidenceEvent、生产密钥托管——
  均在整包 next_step 保留；墓碑持久化走调用方的具名存储方法（本卡未新增表）。
- 影响：ALN-16「实际导出下载与存储删除」欠账落地；隐私删除链
  计划→执行→审计闭环。

审阅结论：changes_requested；f633130修复待其他审阅者复核
整包结论：ALN-16 仍实现中
