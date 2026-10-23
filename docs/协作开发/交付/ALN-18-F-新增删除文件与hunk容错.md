# ALN-18-F 交付：新增/删除文件与 hunk 位置容错

任务：ALN-18-F / 新增/删除文件与 hunk 位置容错
负责人：claude-main
审阅人：待定（负责人不得自审）
基线 commit：`2f09266`（承接 ALN-18-E）；交付前 HEAD 已推进到 `9908aad`（并行会话 ALN-14-A 成果，与本卡无共享文件）
分支与最终 commit：`main`（本地提交，未推送）；`dbef120`（源码+测试+任务卡+本文件）
认领状态：review

## 触发场景与行为变化

- 输入/触发：模型或工具产出的标准 unified diff 中包含新建或删除文件（`/dev/null` 一侧），或待改文件里有重复片段、上下文略有漂移。
- 修改前（`2f09266`）：
  - `/dev/null` 一律拒绝，新增/删除文件只能手工 `edits`。
  - hunk 必须按内容唯一命中；重复片段直接失败，首尾上下文过期即失败。
- 修改后：
  - `--- /dev/null` + `+++ b/<path>` → 原子新建文件（目标必须不存在、父目录必须存在、`.py` 仍过语法门禁）。
  - `--- a/<path>` + `+++ /dev/null` → 原子删除文件（hunk 必须覆盖全部内容，否则拒绝）。
  - 多处命中 → 用 hunk 头新文件起始行号消歧（前序 hunk 已应用，内容按新文件行号定位）；仍不唯一则拒绝。
  - 零命中 → 逐层裁掉首/尾成对上下文行（最多 2 行）后重试；裁剪后仍不唯一或仍无匹配则拒绝。
  - 新建/删除/改写同批原子提交：任一写盘失败全量回滚——新建文件被删除、删除文件按原文恢复、改写文件复原；事务日志记录 `existed`/`deleted`，恢复时创建文件被移除、删除文件被还原，内容偏离则冲突不覆盖。
  - 证据新增 `action`（`created`/`deleted`/`modified`）；不存在一侧的 `sha256_before/after` 为空串。
  - 能力包升至 `1.5.0`，`rollback_version=1.4.0`，工具 ID 仍为八个。

## 范围

- 独占模块：`evoagent/tools/coding.py`（修改）、`tests/test_coding_tools.py`（修改，51 项：既有 37 项 + 新增 14 项）。
- 共享文件补丁：无。未触碰注册表、`contract.py`、`app.py` 或执行路径。
- 契约变化：`coding_patch` 的 `input_schema` 不变（仍为 `edits`/`diff`/`dry_run`），描述更新以声明新增能力；`edits` 模式行为与校验不变。
- 数据迁移：无；旧日志缺少 `existed`/`deleted` 字段时按“改写”语义恢复，向后兼容。

## 验证证据

- 新增文件：`/dev/null` → 新 `.py` 原子创建，`action=created`、`sha256_before=""`；`dry_run` 返回 `--- /dev/null` 预览且不落盘。
- 新增守卫：目标已存在、父目录不存在、旧计数非 0（hunk 含删除行）→ 明确拒绝且目标不变。
- 删除文件：全文移除的 diff 原子删除，`action=deleted`、`sha256_after=""`；只覆盖部分内容 → 拒绝且文件不变。
- 行号消歧：文件含两处相同片段，hunk 指向第 6 行 → 只改第二处；指向不存在的第 3 行 → 拒绝且文件不变。
- 上下文容错：首尾各一行过期上下文 → 裁剪后唯一命中；三行过期上下文超出 2 行上限 → 拒绝且文件不变。
- 行号基准：前序 hunk 增加一行后，后续 hunk 按新文件行号（`@@ -3 +4 @@`）正确定位。
- 混合补丁：改写 + 新建 + 删除同一 diff 原子成功，`action` 依序为 `modified/created/deleted`；删除阶段 `unlink` 失败 → 全量回滚（改写复原、新建删除、删除文件保留）。
- 日志恢复：pending 日志中的新建文件被移除、删除文件被还原；新建文件内容被他人改写 → 冲突不删除。
- 原子性：任一预检失败不写盘、无日志残留。
- 命令与结果：
  - `python -m pytest tests/test_coding_tools.py -q` → `51 passed`（日志 `.planning/2026-10-22-aln18/f-targeted.txt`）。
  - `python -m pytest tests/test_coding_tools.py tests/test_tools.py tests/test_tool_contract_fields.py -q` → `64 passed`（`.planning/2026-10-22-aln18/f-combined.txt`）。
  - 全量 `python -m pytest tests -q` → `1331 passed, 1 skipped`（139.88s）（`.planning/2026-10-22-aln18/f-full-suite.txt`，限定 `tests` 目录）。
  - 台账校验：`verify_claims` / `scan_all_ledgers` / `check_gap_closure` / `check_collaboration_plan` 均通过（后者 `dispatches: 36`、`errors: []`）。
- 边界：仍不支持二进制、权限位/符号链接、`\ No newline at end of file` 标记、空文件 diff、跨文件重命名；行号仅用于消歧（不做任意偏移搜索），上下文裁剪上限 2 行；未做 App 接线。

## 回滚与未完成事项

- 回滚触发：本卡只扩展独占模块；回退 `coding.py` 与测试即恢复 `1.4.0` 行为。
- 能力包回滚：清单指向 `1.4.0`，但 `ToolRegistry` 无卸载语义，实际降级仍待注册表改造。
- 未完成：二进制与权限位、重命名识别、LSP 适配、专家/App 接线、真 mypy 联调、注册表级回滚。
- 影响主题：C-12 仍实现中；ALN-18 整包不关闭。

审阅结论：待审
整包结论：仍实现中

## 提交记录

- `dbef120`：`evoagent/tools/coding.py`、`tests/test_coding_tools.py`、任务卡、本交付文档同批本地提交（未推送）。
- 台账（workboard/认领表）为独立提交。
