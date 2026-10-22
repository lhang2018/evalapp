# ALN-18-A 交付：编程工具包（定位 → 改写 → 验证）

任务：ALN-18-A / 编程工具包：符号定位、受约束改写与检查/测试执行
负责人：claude-main
审阅人：待定（负责人不得自审，待 runtime 线独立复验）
基线 commit：`c885cbd`（认领时 HEAD；验证在工作区含并行在途改动的状态下执行）
分支与最终 commit：`main`（本地提交，未推送）；`acbd22a`（本卡源码与文档同批提交）
认领状态：review

## 触发场景与行为变化

- 输入/触发：模型或工作流需要在真实项目里定位符号、修改代码、运行语法/类型检查与测试。
- 修改前：`evoagent/tools/` 只有通用 `file_read`/`file_write`/`fs_list`/`git_read`；没有 AST/符号定位，`file_write` 不校验语法，也没有类型检查或测试执行适配器。`coding` 专家只有提示词与白名单。
- 修改后：新增 `evoagent/tools/coding.py`，提供五个契约化工具与能力包清单：
  - `coding_symbols`：AST 扫描项目内 Python 文件，返回类/函数定义、限定名与 `file:line`；跳过 `.git`/`node_modules`/`__pycache__` 等目录与越界软链；语法错误文件单列在 `errors`。
  - `coding_edit`：项目根内路径约束；`old_text` 必须唯一命中；`.py` 改写后语法不合法**不写盘**；写后重读比对，不一致则回滚并如实报告。
  - `coding_syntax_check`：`compileall` 子进程；`PYTHONPYCACHEPREFIX` 指向临时目录，不污染项目。
  - `coding_typecheck`：mypy 适配；检查器不可用时返回 `executed=False`、`goal_reached=None`，**不判成功**。
  - `coding_test`：真实 `pytest` 子进程；通过/失败/未收集（rc=5）三态可区分，未收集不判成功。
- 能力包清单 `CODING_PACK`：`pack_id=coding_pack`、`version=1.0.0`、五个 `tool_ids`、`rollback_version=None`、`contract_test_ref`。

## 范围

- 独占模块：`evoagent/tools/coding.py`（新增）。
- 测试：`tests/test_coding_tools.py`（新增，14 项）。
- 共享文件补丁：无。未改 `tools/registry.py`、`experts/registry.py`、`tools/contract.py`、`app.py`、`core/controller.py`、`react/engine.py`。
- 新增契约：五个 `ToolContract`，均声明 `permission_requirements`、`side_effect_level`、`risk_class`、`offline_capable=True`、`requires_network=False`、`cost_model`、`contract_test_ref`。
  - 权限词表（本卡自定，接线时须由集成授权策略确认）：`fs.read`、`fs.write`、`process.exec`。
  - `coding_edit` 显式 `risk_class="high"`（副作用级 local）；`coding_test` 声明 `side_effect_level="external"`、`risk_class="high"`（会执行项目内任意代码，按 fail-closed 对待）。注册表仍按声明与推导的严格者裁决。
- 数据迁移、旧格式兼容：无。
- 未接线：不在 App 启动路径自动注册，也不改 coding 专家白名单；`install_coding_pack(registry, root)` 由测试直接安装到独立 `ToolRegistry` 验证。

## 验证证据

- 正常路径：符号定位命中 `A`、`A.m`、`outer.inner` 与行号；改写成功并重读校验；语法检查通过；假 mypy 检查器通过；pytest 通过。
- 拒绝/失败路径：路径越界、旧文本未命中/命中多处、改写致语法错误、语法检查失败、类型检查器不可用、类型检查失败、pytest 失败、未收集测试，均不得返回 `ok=True`/`goal_reached=True`。
- 命令与结果：
  - `python -m pytest tests/test_coding_tools.py -q` → `14 passed`（8.83s）。
  - `python -m pytest tests/test_coding_tools.py tests/test_tool_contract_fields.py tests/test_tool_health.py tests/test_tools.py -q` → `36 passed`（9.94s），日志 `.planning/2026-10-22-aln18/targeted.txt`。
  - 全量：`python -m pytest -q` → `1176 passed, 1 skipped`（128.99s），日志 `.planning/2026-10-22-aln18/full-suite.txt`。该次运行含并行会话在途改动，且跑在最后一次 `coding_test` 风险声明调整之前；调整后专项 14 项已复跑通过。
- 验证边界：本机未安装 mypy，**可执行分支用假检查器覆盖**（探针/成功/失败三路），真实 mypy 环境仍待联调；pytest 为真实子进程；无网络访问；未在 Android/真机侧验证（本卡是服务端工具包）。

## 回滚与未完成事项

- 回滚触发：本卡未改动任何既有执行路径或启动注册；删除 `evoagent/tools/coding.py` 与测试即完全回退，不影响既有工具。
- 能力包级回滚：清单已声明 `rollback_version`（当前无前序版本，为 `None`）。`ToolRegistry` 目前没有卸载语义，模块级升级/回滚接线留待注册表提供后另卡。
- 保留限制与后续：
  - 符号引用/调用图与跨文件解析未做，当前只给定义与导入集合。
  - LSP 适配未做；类型检查依赖 mypy 安装。
  - 专家接线、App 注册、权限策略授权与执行路径接入另卡（共享文件需串行交接）。
  - 测试执行仅以进程超时约束，未加 OS 级沙箱；高风险确认策略待接线时配置。
- 影响主题：C-12（实现中，未关闭）；ALN-18 整包仍为 `planned`，本卡不宣布整包完成。

审阅结论：待审
整包结论：仍实现中

## 提交记录

- `acbd22a`：`evoagent/tools/coding.py`、`tests/test_coding_tools.py`、任务卡、本交付文档、workboard/认领表同批本地提交（未推送）。
- 本提交记录补充为独立文档提交：`acbd22a` 之后仅更新本文件与 workboard 证据字段，不改源码与测试。
