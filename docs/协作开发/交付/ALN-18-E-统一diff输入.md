# ALN-18-E 交付：统一 diff 输入与原子应用

任务：ALN-18-E / 统一 diff 输入与原子应用
负责人：claude-main
审阅人：待定（负责人不得自审）
基线 commit：`3ea3772`（承接 ALN-18-D）
分支与最终 commit：`main`（本地提交，未推送）；`0ba45fc`（源码+测试+任务卡+本文件）
认领状态：review

## 触发场景与行为变化

- 输入/触发：模型或外部工具直接产出标准 unified diff，希望一次原子应用，而不是手工拆成 `old_text/new_text`。
- 修改前（`3ea3772`）：`coding_patch` 只接受 `{path, old_text, new_text}` 编辑列表。
- 修改后：
  - `coding_patch` 新增可选 `diff` 字符串，与 `edits` 二选一。
  - 解析 `---`/`+++` 文件头、`@@ -a,b +c,d @@`、多文件、多 hunk、`a/`/`b/` 前缀；按 hunk 计数消费行，删除行即使以 `--- ` 开头也不会被误判为文件头。
  - 同一文件多个 hunk 依次应用到内存内容，最终合并为一条 prepared 记录，复用既有全量预检、语法门禁、写后核验、事务日志与回滚。
  - hunk 上下文块必须在当前内容中唯一命中；未命中/多处命中/计数不足/非法头部 → 整体拒绝、不写盘。
  - 明确拒绝 `/dev/null`（新增/删除文件）、`\ No newline at end of file`、空文件 diff、`edits` 与 `diff` 同时提供或都不提供。
  - 能力包升至 `1.4.0`，`rollback_version=1.3.0`，工具 ID 仍为八个。

## 范围

- 独占模块：`evoagent/tools/coding.py`（修改）、`tests/test_coding_tools.py`（修改，37 项：既有 30 项 + 新增 7 项）。
- 共享文件补丁：无。未触碰注册表、`contract.py`、`app.py` 或执行路径。
- 契约变化：`coding_patch` 输入新增可选 `diff`（`input_schema` 已更新，`required` 移除）；`side_effect_level` 等其余字段不变。
- 数据迁移：无；既有 `edits` 调用方行为不变。

## 验证证据

- 正常路径：两文件各一 hunk 的 diff 原子应用，逐文件 64 位摘要与行号完整，日志目录无残留。
- 多 hunk：同一文件两个 hunk 依次应用，结果与逐条 old/new 编辑一致。
- 歧义行：删除行内容为 `-- note`（diff 行以 `--- ` 开头）时按 hunk 计数正确解析并应用。
- CRLF：输入 diff 使用 `\r\n` 时归一化后正常应用（Windows 来源）。
- 原子性：第二文件 hunk 上下文未命中 → 第一文件也不写盘、无日志残留。
- 输入校验：`edits`+`diff` 同给、都不给、`/dev/null`、无换行标记、非法 hunk 头 → 明确拒绝且文件不变。
- dry-run：返回统一 diff，文件不变。
- 命令与结果：
  - `python -m pytest tests/test_coding_tools.py -q` → `37 passed`。
  - `python -m pytest tests/test_coding_tools.py tests/test_tools.py tests/test_tool_contract_fields.py -q` → `50 passed`。
  - 全量 `python -m pytest tests -q` → `1313 passed, 1 skipped`（145.14s），日志 `.planning/2026-10-22-aln18/e-full-suite-rerun.txt`（限定 `tests` 目录，避免 `outputs/` 测试副本的收集冲突）。
  - 首次全量运行出现 1 项 `tests/test_goal_governance.py::test_api_admin_goal_flow_and_operator_denial` 失败（日志 `.planning/2026-10-22-aln18/e-full-suite.txt`）：该测试单文件运行 `53 passed`、全量复跑通过，且与编程包无调用关系；期间共享 `app.py`/`goals_api.py` 有并行会话在途改动，判定为本地 API 端口/线程竞态而非本卡回归。
- 边界：hunk 头中的行号只作证据提示，实际按内容唯一命中定位（不做模糊偏移）；不支持新增/删除文件、二进制、权限位变更；同一路径出现多段文件头会被“单文件一条编辑”规则拒绝；未做 App 接线。

## 回滚与未完成事项

- 回滚触发：本卡只扩展独占模块；回退 `coding.py` 与测试即恢复 `1.3.0` 行为。
- 能力包回滚：清单指向 `1.3.0`，但 `ToolRegistry` 无卸载语义，实际降级仍待注册表改造。
- 未完成：按 hunk 行号容错定位、新增/删除文件、二进制与权限位、LSP 适配、专家/App 接线、真 mypy 联调。
- 影响主题：C-12 仍实现中；ALN-18 整包不关闭。

审阅结论：待审
整包结论：仍实现中

## 提交记录

- `0ba45fc`：`evoagent/tools/coding.py`、`tests/test_coding_tools.py`、任务卡、本交付文档同批本地提交（未推送）。
- 台账（workboard/认领表）为独立提交。
