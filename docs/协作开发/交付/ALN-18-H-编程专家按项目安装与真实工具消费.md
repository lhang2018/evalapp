# ALN-18-H 编程专家按项目安装与真实工具消费（本地组件切片）

> 2026-10-26 接续审查：项目工具表共享宿主当前权限控制器/存储，deny/未确认拒绝改写、明确确认可执行。任务授权/专家自动选择、真实项目与升级恢复仍待。 本轮固定相关358项通过，修复提交0ba37e1/01766f5，新增修复待其他审阅人复核。下文修改前后及首轮验证是历史记录，最新行为以[复核报告](../验收/2026-10-26-新增宿主与音视频费用复核.md)为准；不提升整卡或生产接受。

任务：ALN-18-H / 编程专家按项目安装与真实工具消费（阶段2，runtime 线）
负责人：dsh-lead（本会话认领；按"按优先级顺序认领"接唯一剩余 READY 卡）
审阅人：待定（未分配独立审阅人，不自批）
基线 commit：`eed09f46ea11ceb38fc38b9fcb674b311254734f`（认领时 HEAD；基线 published `21899e6`）
最终 commit：`f43c8efd5135d09d521b39b4d4d1bb315023cd33`（tree `66b80b87093dfb3342f083591583875fdada26bd`）
认领状态：claimed → **review（本地组件切片；真实项目集成未做，不冒称通过）**

## 触发场景与行为变化

- 触发：编程专家需要**按受信项目根**装配既有精确版本编码包，并在受限根内执行真实语法/项目测试工具；不能用全局注册表跨项目覆盖。
- 修改前：`tools/coding.py` 已有 `install_coding_pack(registry, root)` / `CODING_PACK`（精确版本、8 个 coding 工具、根约束与风险契约），但**没有按项目根的独立装配与真实工具消费入口**；App 未装配任何编码包消费者。
- 修改后（新增 [experts/coding_consumer.py](evoagent/experts/coding_consumer.py)）：
  1. `install(project_root, expected_version, actor, reason)`：校验根为真实目录、**拒绝符号链接根**（链接替换）、版本必须等于 `CODING_PACK.version`、记录 actor/reason；同根同版本**幂等**；每个根创建**独立 ToolRegistry** 并安装既有编码包；
  2. 安装异常 → **实际回滚**（丢弃半装注册表；首装回到未安装；已有版本恢复并重新执行语法检查），并返回磁盘摘要变化；
  3. `execute(project_root, tool_id, args)`：未安装根拒绝（跨根）、未知工具拒绝；通过该根独立注册表执行真实 `coding_syntax_check` 等既有工具，记录 `returncode/duration/evidence` 与 `.py` 文件摘要变化；不自动安装外部依赖、不提交代码、不扩大授权；
  4. `status()` 查询按根安装清单；`app.py` 装配 `app.coding_consumer`。

## 范围

- 独占模块：`evoagent/experts/coding_consumer.py`（新增）、`tests/test_coding_expert_consumer.py`（新增）。
- 共享文件补丁（精确修改点）：`evoagent/app.py`（+3 行装配）。
- 未改：`tools/coding.py`（100% 复用既有包与工具）、`core/controller.py`（任务→专家自动选中未接，见缺口）、`tools/registry.py`。
- 兼容：纯新增服务与 App 属性；无 schema/迁移。

## 验证证据（首轮历史记录）

- 专项：`python -m pytest tests/test_coding_expert_consumer.py -q` → **6 passed**：
  - 真实项目根安装编码包并执行 `coding_syntax_check`（returncode=0）；
  - 同根重复安装幂等；
  - 双根独立注册表：根 A 语法正常、根 B 语法错误按实际返回码拒绝；
  - 未安装根/版本不符/未知工具/符号链接根 → 明确拒绝（无符号链接权限环境自动跳过该路径）；
  - 注入安装失败 → 实际回滚（未安装状态、磁盘无变化），恢复安装后重新检查通过；
  - App 装配 `coding_consumer`。
- 实测证据：`.planning/2026-10-26-aln18h/coding-consumer-evidence.json`，**9 项检查全过**（安装/真实返回码/幂等/双根独立/三类拒绝/磁盘变化记录）。
- 全量后端：`python -m pytest tests/ -q` → **2381 passed / 0 failed / 1 skipped（382.42s）**，日志 `.planning/2026-10-26-aln18h/backend-tests.txt`。
- **真实项目边界（如实登记，未冒称通过）**：
  - 用户**尚未选定临时真实项目/固定测试依赖**，因此「从实际任务选中专家 → 项目安装 → 真实编译/语法与项目测试」的**项目级集成未做**；本地测试使用临时目录作受限根；
  - 任务→编程专家的**自动选中/控制器接线未做**（卡片允许先做本地组件与拒绝路径；`core/controller.py` 未改）；
  - 回滚路径以**注入安装失败**验证；编码包升级失败/半写入外部项目场景未构造；
  - 磁盘变化只统计 `.py` 摘要（既有工具把 `__pycache__` 写到临时目录，不落项目根）。

## 回滚与未完成事项

- 回滚：`git revert f43c8efd5135d09d521b39b4d4d1bb315023cd33`（删消费者/测试与 App 3 行装配；无 schema 变更）。
- 保留缺口：
  1. **真实项目集成**（用户选定临时项目 + 固定测试依赖后）未做——卡片的正式验收条件未满足；
  2. 任务→专家自动选中与控制器接线未做（需结合 18-G 专家路由与 23-A 任务契约联调）；
  3. 编码包升级/半写入回滚场景未构造；
  4. 未采真机/生产证据。
- 影响：ZG-07 的"按项目独立装配 + 拒绝/回滚路径"本地组件交付；真实项目集成仍待用户提供项目后另验。

## 审阅结论：待审（本地组件切片；不替代真实项目验收）

## 整包结论：仍实现中（真实项目、控制器自动选中、升级回滚与生产证据待补）
