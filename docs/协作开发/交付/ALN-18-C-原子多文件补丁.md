# ALN-18-C 交付：原子多文件补丁与 dry-run 预览

任务：ALN-18-C / 原子多文件补丁与 dry-run 预览
负责人：claude-main
审阅人：待定（负责人不得自审）
基线 commit：`48b4aba`（承接 ALN-18-B；期间并行提交 `6c90901` 不影响本卡独占模块）
分支与最终 commit：`main`（本地提交，未推送）；待定
认领状态：review

## 触发场景与行为变化

- 输入/触发：模型需要一次改动多个文件，且不希望第二处失败留下半套改动。
- 修改前（`48b4aba`）：`coding_edit` 一次只改一个文件；多文件只能逐个调用，中途失败会留下部分已写文件。
- 修改后：
  - 新增 `coding_patch`：一次提交最多 50 条 `{path, old_text, new_text, expected_sha256?}`，每个文件最多一条。
  - 预检覆盖路径边界、文件存在与大小、旧文本唯一命中、摘要前缀匹配、no-op、`.py` 改写后语法；任一失败直接返回，**全部不写盘**。
  - 提交后逐文件重读比对；不一致则回滚全部已写文件；回滚失败显式 `side_effect_uncertain=true`，不报成功。
  - `dry_run=true` 返回逐文件统一 diff、前后完整 `sha256` 与预计行号，不写盘。
  - 成功证据逐文件给出 `file`、`line`、完整 `sha256_before/after`，可直接作为下一次 `expected_sha256`。
  - 能力包升至 `1.2.0`，`rollback_version=1.1.0`，工具 ID 七个。

## 范围

- 独占模块：`evoagent/tools/coding.py`（修改）、`tests/test_coding_tools.py`（修改，25 项：既有 17 项 + 新增 8 项）。
- 共享文件补丁：无。未触碰注册表、`contract.py`、`app.py` 或执行路径。
- 契约变化：新增 `coding_patch`（`side_effect_level=local`、`risk_class=high`、`fs.read`/`fs.write`、离线）；其余工具输入不变。
- 数据迁移：无。

## 验证证据

- 正常路径：两文件补丁全部写盘，`applied=["a.py","b.txt"]`，逐文件 64 位摘要与行号完整；用 `sha256_after` 前缀再次改写成功。
- dry-run：返回 `-x = 1` / `+x = 2` 统一 diff 与前后摘要，文件内容保持原样，证据 `written=[]`。
- 原子性：第二条旧文本不存在 / 第二条改写后语法错误 / 第二条摘要过期 / 重复路径 / no-op 均拒绝，第一条也不写盘。
- 回滚：写后校验不一致（模拟第二文件被截断）回滚全部文件并报告 `rolled_back`；回滚自身失败时报 `side_effect_uncertain=true`、`independently_verified=false`。
- 命令与结果：
  - `python -m pytest tests/test_coding_tools.py -q` → `25 passed`。
  - `python -m pytest tests/test_coding_tools.py tests/test_tools.py tests/test_tool_contract_fields.py -q` → `38 passed`；`tests/test_coding_tools.py tests/test_tools.py` → `32 passed`。
  - 全量 `python -m pytest tests -q` → `1226 passed, 1 skipped`（127.05s），日志 `.planning/2026-10-22-aln18/c-full-suite-final.txt`（限定 `tests` 目录，避免 `outputs/` 测试副本的收集冲突）。
  - 台账校验：`tools/verify_claims.py`、`tools/scan_all_ledgers.py`、`tools/check_gap_closure.py`、`tools/check_collaboration_plan.py` 均通过，日志 `.planning/2026-10-22-aln18/c-validators.txt`。
- 边界：静态文本匹配，非补丁格式（unified patch 输入不支持）；不做跨文件语义校验（仅逐文件 `.py` 语法）；未在真实 LSP/IDE 环境对照，未做 App 接线。

## 回滚与未完成事项

- 回滚触发：本卡只扩展独占模块；回退 `coding.py` 与测试即恢复 `1.1.0` 行为。
- 能力包回滚：清单指向 `1.1.0`，但 `ToolRegistry` 无卸载语义，实际降级仍待注册表改造；不做虚假回滚演示。
- 未完成：unified patch 输入、跨文件依赖/类型级校验、写盘中断的持久化事务日志、LSP 适配、专家/App 接线、真 mypy 联调。
- 影响主题：C-12 仍实现中；ALN-18 整包不关闭。

审阅结论：待审
整包结论：仍实现中

## 提交记录

- 待定：`evoagent/tools/coding.py`、`tests/test_coding_tools.py`、任务卡、本交付文档同批本地提交（未推送）。
- 台账（workboard/认领表）为独立提交。
