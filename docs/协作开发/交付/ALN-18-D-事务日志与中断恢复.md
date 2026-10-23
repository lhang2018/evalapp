# ALN-18-D 交付：补丁事务日志与中断恢复

任务：ALN-18-D / 补丁事务日志与中断恢复
负责人：claude-main
审阅人：待定（负责人不得自审）
基线 commit：`2ebd9c0`（承接 ALN-18-C）
分支与最终 commit：`main`（本地提交，未推送）；`3ea3772`（源码+测试+任务卡+本文件）
认领状态：review

## 触发场景与行为变化

- 输入/触发：多文件补丁写盘途中进程被杀（断电、OOM、Ctrl-C），重启后需要知道改了哪些文件、恢复到什么状态。
- 修改前（`2ebd9c0`）：`coding_patch` 只有进程内回滚；进程消失后无持久化记录，可能留下半套改动。
- 修改后：
  - 写盘前把事务日志原子落盘（临时文件 + `os.replace`）到项目根 `.evoagent_patch_journal/`，记录每个文件的 `path`、`sha256_before/after` 与原始内容。
  - 成功并核验后先把日志标记 `committed` 再删除；进程内回滚成功也删除；回滚失败保留日志供恢复。
  - 新增 `coding_patch_recover`：`pending` 日志按摘要判定——等于 `after` 恢复原文、等于 `before` 视为已恢复、两者都不等记为冲突且**不覆盖**；全部解决才删除日志。`committed` 日志只清理，不回滚已确认结果。
  - `dry_run=true` 只报告动作，不写盘、不删日志；损坏日志跳过并报告，不阻断其他日志。
  - 日志目录加入符号扫描跳过集合；成功路径不留残留（空目录移除）。
  - 能力包升至 `1.3.0`，`rollback_version=1.2.0`，工具 ID 八个。

## 范围

- 独占模块：`evoagent/tools/coding.py`（修改）、`tests/test_coding_tools.py`（修改，30 项：既有 25 项 + 新增 5 项）。
- 共享文件补丁：无。未触碰注册表、`contract.py`、`app.py` 或执行路径。
- 契约变化：新增 `coding_patch_recover`（`side_effect_level=local`、`risk_class=high`、`fs.read`/`fs.write`、离线）；`coding_patch` 行为增加日志，输入不变。
- 数据迁移：无；旧版本产生的项目没有日志目录，恢复工具直接返回空结果。

## 验证证据

- 正常路径：补丁成功后 `.evoagent_patch_journal/` 无残留文件。
- 中断恢复（模拟）：放置 `pending` 日志且文件处于 `after` 状态 → 恢复原文、`restored=1`、删除日志；再次调用为空操作成功。
- 冲突保护：文件既非 `before` 也非 `after` → 不覆盖、`conflicts=1`、日志保留；损坏 JSON 跳过并报告，其他日志照常处理。
- `committed` 日志：保留已应用结果并清理日志。
- 已恢复态：文件处于 `before` 时 `action=already_before` 且清理日志。
- dry-run：报告恢复计划，文件与日志都不变。
- 回滚路径：写后核验不一致且回滚成功时日志删除；回滚自身失败时日志保留并在证据中给出日志名。
- 命令与结果：
  - `python -m pytest tests/test_coding_tools.py -q` → `30 passed`。
  - `python -m pytest tests/test_coding_tools.py tests/test_tools.py tests/test_tool_contract_fields.py -q` → `43 passed`。
  - 全量 `python -m pytest tests -q` → `1268 passed, 1 skipped`（176.65s），日志 `.planning/2026-10-22-aln18/d-full-suite.txt`（限定 `tests` 目录，避免 `outputs/` 测试副本的收集冲突；计数含并行会话同期新增测试）。
- 边界：恢复只信任本工具写入的日志格式；日志内含原文，体积与改动内容成正比；未做跨设备/网络文件系统的原子性验证；未做 App 接线。

## 回滚与未完成事项

- 回滚触发：本卡只扩展独占模块；回退 `coding.py` 与测试即恢复 `1.2.0` 行为，遗留日志目录可手工删除。
- 能力包回滚：清单指向 `1.2.0`，但 `ToolRegistry` 无卸载语义，实际降级仍待注册表改造。
- 未完成：写盘日志的压缩/加密、跨平台文件锁、unified patch 输入、LSP 适配、专家/App 接线、真 mypy 联调。
- 影响主题：C-12 仍实现中；ALN-18 整包不关闭。

审阅结论：待审
整包结论：仍实现中

## 提交记录

- `3ea3772`：`evoagent/tools/coding.py`、`tests/test_coding_tools.py`、任务卡、本交付文档同批本地提交（未推送）。
- 台账（workboard/认领表）为独立提交。
