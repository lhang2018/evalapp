# ALN-18-G 交付：App 与编程专家真实消费能力包

任务：ALN-18-G App 与编程专家真实消费能力包
负责人：zcode-next
审阅人：待定（须独立复核，不接受自审自收）
基线 commit：`b4fb864`（认领锚点，因 12-V 真机被用户占用转 blocked 让位）；分支 feature/user-direction，交付 commit 见 git log "feat(ALN-18-G)"
认领状态：delivered（review，待独立复核）

## 触发场景与行为变化

- 输入/触发：真实项目（磁盘上的真实 Python 文件）通过编程专家/
  用户任务消费 18-A~F 能力包。
- 修改前：`install_coding_pack` **零生产消费者**（全库 grep 仅定义），
  编码工具只有单元级验证，从未进入真实执行路径。
- 修改后（新增 `evoagent/tools/coding_consumption.py`）：
  1. **包安装绑定**：`install_for_task(task_id, root)` 把包装进指定
     注册表并产出绑定记录（pack_id/version/tool_ids/rollback_version/
     root）——与 06-R 精确消费语义对齐；安装到哪个注册表，哪个
     注册表可执行（消费语义被测试显式固化）。
  2. **真实消费链（E2E 测试走真实执行内核）**：ReactEngine +
     ToolRegistry + 真实项目文件 + ScriptModel 驱动
     `coding_edit`（跨文件修改 calc/ops.py）→ `coding_references`
     → finish——磁盘文件真实变化（以文件内容断言，不以工具返回值）。
  3. **验收/回滚闭环**：破坏性修改（语法错误）→ `coding_syntax_check`
     真实失败 → `restore_snapshot` 恢复（文件内容断言恢复）；
     `changed_paths` 明确列出快照后新建文件（不越权删除未知文件）。
  4. **产物归档消费 24-A**：`archive_modified` 把实际修改文件经
     ArtifactStore 归档（source_path 记录），可读回（downloadable 链）。
- 未宣称 LSP：绑定记录与包身份均无 lsp/language-server 字段
  （专项断言）；coding_symbols/references 保持 18-A 的 AST 级口径。

## 范围

- 独占新增：`evoagent/tools/coding_consumption.py`
  （CodingPackRuntime/snapshot_project/restore_snapshot）、
  `tests/test_coding_consumption.py`（6 项，含真实执行内核 E2E）。
- 小改：无既有文件改动（install_coding_pack/CODING_PACK 原样复用；
  coding.py 一行未改）。
- 缺口：controller/App 自动装配（app.py 不在本卡写入边界）——
  生产挂载留缺口，同 06-R/07-B 模式；android-app/src 端侧消费
  （编码任务下发 UI）属后续卡。

## 验证证据

- 专项：`python -m pytest tests/test_coding_consumption.py -q` →
  **6 passed**（绑定身份/注册表消费语义/E2E 跨文件修改以磁盘事实
  断言/快照回滚闭环（语法失败→恢复）/产物归档可读回/无 LSP 宣称/
  非法根拒绝）。
- 相关回归：test_coding_tools（18-A~F 契约）+ test_asset_consumption
  （06-R）+ test_delivery_artifacts（24-A）→ **74 passed**。
- 全量：`python -m pytest tests -q --ignore=tests/test_goal_budget_migration.py`
  → **2022 passed / 1 skipped / 0 failed**。
- 边界：受控真实项目=临时目录内真实 Python 文件与真实 AST/语法
  检查执行；"受控真实项目"未含外部依赖安装/多语言项目；执行内核
  的风险确认按测试约定放行（high_risk_mode=allow，交付文档如实
  标注）。真机/生产项目联测另验。

## 回滚与未完成事项

- 回滚：revert 单提交（纯新增文件）。
- 未完成（按任务卡原范围保留）：
  1. controller/App 自动装配（编程专家选中时按 target_object 安装
     编码包）——app.py 串行合入后一行接线；
  2. coding_test 工具的 pytest 子进程在消费链 E2E 中未用（验收用
     syntax_check 代替——真实项目测试运行属环境更重的场景）；
  3. 多语言项目、外部依赖安装、LSP 明确不做（未宣称）。
- 影响主题：F02/A-60、C-12（AST 级验证）、C-05（能力包消费）、
  06-R（精确版本消费）衔接。

## 审阅结论

待审。建议重点：snapshot_project 的文件数上限与 skip_dirs 是否
适合真实项目；restore_snapshot 不删除快照后新建文件的安全取舍；
install 幂等语义（重复安装覆盖同 id 契约）。
整包结论：仍实现中。
