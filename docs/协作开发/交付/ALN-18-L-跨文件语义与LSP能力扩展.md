# ALN-18-L 交付：跨文件语义与 LSP 能力扩展

2026-10-26接续修复b5b376d：导入别名重复绑定、包导出遮蔽及属性/字典式重绑不再误作模块身份；不确定时澄清且无计划，正常明确模块导入保留。相关83项通过，含实际临时项目应用与运行；详见[接续复核](../验收/2026-10-26-反馈私钥与模块身份复核.md)。仍是受限静态分析，非语义LSP，新修复待复核。

任务：ALN-18-L（PRODUCT-2026-10-25，父包 ALN-18，阶段5）
负责人：zcode-main；审阅人：待定（须独立复核）
基线 commit：`e16dbfeedd48d3e58cc24d68886eb76f2d7770d7`（接手锚点，非已发布基线）
分支与最终 commit：feature/user-direction（源码随本卡提交落库）
认领状态：delivered（review，待独立复核）

## 目标与行为变化

按真实编程任务引入**跨文件语义验证**与**固定版本语言服务**：重构前先做跨文件引用
分析，不确定就澄清；诊断在修改前后对照。新增独占模块 `evoagent/tools/refactor.py`，
不重新实现已接受切片，不自行写盘（改写交给既有原子工具 `coding_patch`）。

## 范围

- 独占新增：`evoagent/tools/refactor.py`（跨文件引用分析 + 语言服务版本门禁 + 诊断对照）。
- 测试：`tests/test_refactor.py`（12 项）。
- 共享文件：无（不改 `coding.py`/`registry.py`/`experts/registry.py` 等已登记共享文件）。

## 验收对齐

1. **固定语言服务版本**：`LANGUAGE_SERVICE_ID="python-ast"`、
   `LANGUAGE_SERVICE_VERSION=<解释器版本>`；`language_service_status()` 不可用即
   `available=False`，且 `is_semantic=False` 明示**未实现语义级 LSP、不宣称支持**。
2. **跨文件语义验证**：`analyze_rename` 用 `tokenize` 精确定位符号全部出现（跨模块），
   排除字符串/注释中的同形词；计划为每文件整文件一条编辑，可直接交 `coding_patch`
   原子应用。
3. **诊断/修改前后对照**：`collect_diagnostics`（语法级，固定解释器版本）+
   `compare_diagnostics` 输出新增/消失/存留（多重集计数）。
4. **无法确定的重构请求澄清**：跨模块重名、星号导入、动态引用
   （getattr/globals/eval/exec…）、新名冲突、不可解析文件、符号不存在、空改写
   一律返回 `needs_clarification` 与澄清问题，不产可执行计划、不猜测。

## 验证证据

- 新增专项：`tests/test_refactor.py` **12 passed**。
- 联合回归：`pytest tests/test_refactor.py tests/test_coding_tools.py -q` → **63 passed**
  （含真实 `coding_patch` 应用闭环与编码工具包既有回归）。
- 后端全量：`python -m pytest tests/ -q` → **2072 passed / 13 failed / 1 skipped**。
  - 13 个失败全部位于并行会话在途文件 `tests/test_developed_review_3.py`（10 项）、
    `tests/test_release_install_retire.py`（1 项）与 `tests/test_attachments.py`（1 项）+
    附件用例，与本卡无关：本卡仅新增 `evoagent/tools/refactor.py`，未被
    `coding.py`/`registry.py` 引用，不改变工具契约或运行路径。
  - 日志：`.planning/2026-10-25-aln18l/backend-tests.txt`。
- 关键闭环证据：`test_apply_plan_with_real_patch_tool` 在真实 temp 项目上通过
  `coding_patch` 完成跨两文件重命名，断言注释与字符串中的同形词**未被改写**，
  改写后项目无语法诊断。

## 回滚与未完成事项

- 回滚：删除 `evoagent/tools/refactor.py` 与 `tests/test_refactor.py` 即可；无迁移、
  无契约变更。
- 保留缺口：**语义级 LSP（真实语言服务器）未接入**，本卡仅提供固定版本门禁与
  语法级诊断；把 `refactor.py` 注册为能力包工具并接线到编程专家运行时另卡；
  重构计划中同一行多处出现的列替换已按降序处理，但跨行/多编辑合并仍建议由
  patch 事务层统一承担。
- 影响主题：A-60 / A-70；父包 ALN-18 关闭另按整包依赖推进。

审阅结论：待审
整包结论：仍实现中


## 第五批复核（2026-10-25）

复核源码3331898fcddb78b4b271db152fbd408b7c981889。以AST/symtable/显式导入约束重命名，未知身份与作用域拒绝生成计划；f-string、CRLF补丁消费、编译级诊断、外部链接和依赖范围均补边界测试。仍非语义LSP，复杂语义/版本约束/能力包运行装配不宣称完成。

新修复待独立复查，详见[第五批复核](../验收/2026-10-25-第五批补绑凭据与重构复核.md)。
