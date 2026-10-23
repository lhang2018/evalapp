# ALN-18-B 交付：跨文件引用/调用图与并发安全改写

任务：ALN-18-B / 跨文件引用/调用图与并发安全改写
负责人：claude-main
审阅人：待定（负责人不得自审）
基线 commit：`cabf775`（codex 重整认领时的源码锚点；本卡只改独占模块）
分支与最终 commit：`main`（本地提交，未推送）；`48b4aba`（源码+测试+任务卡+本文件）
认领状态：review

## 触发场景与行为变化

- 输入/触发：模型需要知道「这个符号在哪被用/被调用」，以及在并行工作区中安全改写。
- 修改前（`acbd22a`）：`coding_symbols` 只给定义与导入集合；`coding_edit` 只核对旧文本，读后若文件被其他会话修改，仍按旧内容改写。
- 修改后：
  - 新增 `coding_references`：返回定义、`import_edges`、使用/调用点（`file:line`、上下文、置信度）。同文件与已导入定义模块的 `Name` 使用为 high，`Attribute` 为 medium，未建立导入关系的同名符号为 low，不把启发式当类型解析。
  - `coding_edit` 支持 `expected_sha256`（至少 8 位前缀）：与当前文件摘要不符即拒绝写盘并报告期望/实际；写后证据输出完整 `sha256_before/after`，可作为下一次保护输入。
  - 符号结果补充函数签名与返回注解。
  - 能力包清单升至 `1.1.0`，`rollback_version=1.0.0`，工具 ID 增至六个；`1.0.0` 对应不含 `coding_references` 的首片。

## 范围

- 独占模块：`evoagent/tools/coding.py`（修改）、`tests/test_coding_tools.py`（修改，17 项）。
- 共享文件补丁：无。未触碰注册表、`contract.py`、`app.py` 或执行路径。
- 契约变化：新增 `coding_references`（`side_effect_level=none`、`risk_class=low`、`fs.read`、离线）；`coding_edit` 输入增加可选 `expected_sha256`，其余字段不变。
- 数据迁移：无。

## 验证证据

- 正常路径：跨文件 `from pkg.a import target` → 使用点判 high 且 `kind=call`；`import pkg.a` + `pkg.a.target()` → medium；同文件递归/调用判 high。
- 拒绝/并发路径：过期摘要（完整 64 位）拒绝且文件不变；当前摘要前 12 位前缀通过并正常改写；写后证据含完整摘要。
- 命令与结果：
  - `python -m pytest tests/test_coding_tools.py -q` → `17 passed`。
  - 联合既有工具测试 → `39 passed`，日志 `.planning/2026-10-22-aln18/b-targeted.txt`。
  - 全量 `python -m pytest tests -q` → `1179 passed, 1 skipped`（159.75s），日志 `.planning/2026-10-22-aln18/b-full-suite.txt`。注：仓库根的 `outputs/status-2026-10-23-becfad7/`（gitignore 的验证工作区）含 tests 副本，裸 `pytest -q` 会报 import file mismatch；限定 `tests` 目录运行。
- 边界：引用解析是静态启发式，不做类型推断/跨包重导出解析；未在真实 LSP/IDE 环境对照，未做 App 接线。

## 回滚与未完成事项

- 回滚触发：本卡只扩展独占模块，未改执行路径；回退 `coding.py` 与测试即恢复 `1.0.0` 行为。
- 能力包回滚：清单已指向 `1.0.0`，但 `ToolRegistry` 无卸载语义，实际降级仍待注册表改造；不做虚假回滚演示。
- 未完成：类型解析级引用、跨包重导出/动态导入、LSP 适配、专家/App 接线、真 mypy 联调。
- 影响主题：C-12 仍实现中；ALN-18 整包不关闭。

审阅结论：待审
整包结论：仍实现中

## 提交记录

- `48b4aba`：`evoagent/tools/coding.py`、`tests/test_coding_tools.py`、任务卡、本交付文档同批本地提交（未推送）。
- 台账（workboard/认领表）为独立提交，含同期并行会话的认领重整内容，原样保留并在提交说明中注明。
