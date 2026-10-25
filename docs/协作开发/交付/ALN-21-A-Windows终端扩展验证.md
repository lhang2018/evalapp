# ALN-21-A Windows 终端扩展验证

任务：ALN-21-A / Windows或平板扩展验证
负责人：claude-main（本会话认领）
审阅人：待定（未分配独立审阅人，不自批为独立验收）
基线 commit：`2e1438f4ee7583232f11297b51a6cca838b0becb`（认领时 HEAD，不是已发布共同基线）
认领状态：claimed → **review**

## 触发场景与行为变化

- 输入/触发：Android 稳定后，按原 ALN-21 范围验证**后续终端**（本卡取 Windows，"Windows 或平板"之二选一）的部署与状态恢复——不是文档推演，是在本机真实执行。
- 修改前（证据状态）：
  - 17-P 已交付空机部署与组合恢复（accepted），但自述两个保留缺口：**未接入 CLI/API**、**未消费 11-P 的签名与回执**（当时 11-P 在途）；
  - **回执对账层的平台词汇表只有 `("android", "ios")`**——Windows 终端一调 `reconcile(platform="windows")` 即 `ValueError: 不支持的平台`，本卡"回执"验收在现有实现上直接不可执行；
  - 身份登记簿（11-P `IdentityRegistry`，公钥/撤销态 JSON）与三库备份的边界从未在终端场景验证过。
- 修改后（本卡仅两行加法 + 验证资产，不改其他产品行为）：
  - `evoagent/release/manager.py`：`_SUPPORTED_PLATFORMS = ("android", "ios", "windows")`（manifest 平台校验门）；
  - `evoagent/release/delivery.py`：`PLATFORMS = ("android", "ios", "windows")`（回执对账平台门）；
  - 两处均为纯增量，android/ios 现有行为不变（回归证据见下）；
  - `tests/test_release_signing.py` 一处理解性适配：原 `["windows"]` 参数化用例断言的正是"windows 不受支持"，随词汇表扩展改为仍不受支持的 `["linux"]`，保持"不支持的平台必须被拒且错误可读"的原语义（归因实证见下）；
  - 新增 `tests/test_windows_terminal_validation.py`（16 项，可复跑）与实测探针 `.planning/2026-10-25-aln21a/probe.py`（证据工件 `windows-terminal-evidence.json`）。

## 验收矩阵（本机真实执行，非夹具）

终端即本机 Windows（`DESKTOP-4O44IBU`，Windows 10 10.0.19041，Python 3.10.11，源码 commit 见证据 JSON）。

| 验收项 | 场景 | 结果 | 证据 |
|---|---|---|---|
| 固定端版本 | 空机部署 bootstrap → deployment.json 钉住 app_version/source_commit/schema；verify 核对 | `verify_ok=true`，schema 3，版本基线落清单 | evidence `pinned_version` |
| 固定端版本 | 删 events.db / 制造 schema 漂移 | `verify.ok=false` 且错误可读（fail-closed） | 测试 `test_verify_fails_closed_*` |
| 固定端版本 | 伪造 schema 高于程序的备份 → 组合恢复 | `IncompatibleBackup`，**恢复前拒绝且数据零变化** | 测试 `test_incompatible_backup_refused_before_touching_data` |
| 身份 | 组合恢复后实例身份 | `identity_preserved=true`、counts 匹配、实例 ID 与部署清单一致 | evidence `composite_recovery` |
| 身份 | 登记簿与三库备份的边界 | 备份 zip **不含** identity.json；恢复**不回滚**登记簿（备份后登记的轮换在恢复后依然有效——撤销态不得被数据恢复复活） | 测试 `test_identity_registry_is_outside_three_db_backup` |
| 身份 | 登记簿丢失/缺失时的降级 | 文件丢失 → 空登记簿 → `unregistered`，激活被拒；无登记簿服务 → 显式 `no_registry` 并记录，**不静默信任**（03-P：端侧 TrustedKeyRing 才是强制关卡） | 测试 `test_registry_loss_degrades_explicitly_not_silently` |
| 回执 | Windows 平台对账 → 激活回执 | `decision=update_available`；回执绑定服务端解析身份 `win-prod-key-1`/`usable`，制品摘要一致 | evidence `identity_and_receipts` |
| 回执 | 退役版本激活回执 | 拒绝："目标版本已退役，拒绝安装回执" | evidence + 测试 |
| 回执 | 端侧自报密钥与清单不一致 | 拒绝（服务端权威，不采信自报） | 测试 `test_self_reported_key_mismatch_rejected` |
| 状态恢复 | **真实进程击杀**（子进程持任务驻留时 `proc.kill()`，非优雅退出） | 击杀前 RUNNING；新终端实例（重注册工具契约与流程定义后）显式接管，**恢复后 SUCCEEDED**，任务行完整 | evidence `restart_recovery`（killed_exitcode=1） |
| 状态恢复 | 组合恢复真实 RTO/RPO | `ok=true`、身份保持、RTO 0.03s / RPO 0.88s、损失窗口记录 | evidence `composite_recovery` |
| 不复制第二套执行权威 | 恢复后任务账本 | 无重复任务行（ID 唯一）、计数回到备份点 | 测试 `test_recovery_leaves_single_task_ledger` |
| 不复制第二套执行权威 | 安装状态来源 | 全新 `ReleaseDelivery` 实例从同一 store 重建出完全一致的安装态（无内存态权威） | 测试 `test_installation_state_derives_from_single_store` |
| 不复制第二套执行权威 | 事件账本 | 恢复后事件数回到备份点，单 events.db，无第二事件库 | 测试 `test_event_ledger_is_single_authority` |

**真实副作用工具的恢复语义**（外部副作用被击杀后不自动续跑、挂起待对账、force 才重放）由已验收的 `tests/test_process_recovery.py` 覆盖，本卡不重复实现，仅交叉引用。

## 终端差异矩阵（Windows vs Android，显式记录）

| 维度 | Windows 终端（本卡验证） | Android 终端（12-A/12-V/12-E 已验证） |
|---|---|---|
| 终端形态 | CLI / `serve` API 宿主 | 手机 App |
| 部署与恢复 | 17-P bootstrap + 组合恢复，本卡实测 | 12-V 真机重启矩阵 |
| 进程死亡恢复 | `proc.kill()` 真实击杀 + 租约过期接管（本卡） | 系统重启 `adb reboot`（12-V） |
| 回执对账 | 本卡起 `platform="windows"` 可用 | 既有通道 |
| 通知/离线队列 | 无（CLI 在线宿主，stdout 即提示） | core/alerts + 离线存储（12-V） |
| 安装台账 | 无端侧安装台账概念（CLI 不安装制品） | 11-P InstallJournal（server_confirmed 语义） |
| 外部 Runtime pin | 20-P Ollama pin 复用（版本+digest，fail-closed） | 同左 |

## 范围

- 共享文件补丁（各一行，纯增量）：
  - `evoagent/release/manager.py`：`_SUPPORTED_PLATFORMS` 增加 `"windows"`；
  - `evoagent/release/delivery.py`：`PLATFORMS` 增加 `"windows"`；
  - `tests/test_release_signing.py`：参数化用例 `["windows"]` → `["linux"]`（语义保持，注释说明）。
- 新增：`tests/test_windows_terminal_validation.py`（16 项）、`.planning/2026-10-25-aln21a/{probe.py,windows-terminal-evidence.json,mutation_test.py,mutation-results.log,iso-full-server.log}`。
- 数据迁移/兼容：无新增表、无迁移。
- 未改：`app.py`、`api/`、`cli.py`、Android 侧任何文件、`core/backup.py`、`recovery/`、`deployment/`（恢复与部署机制全部复用 17-P 已验收切片）。

## 验证证据

- 本卡专项：`python -m pytest tests/test_windows_terminal_validation.py -q` → **16 passed**（连跑 4 次稳定，含真实进程击杀场景）；
- 变更影响面回归：`test_release_signing.py` 22 passed、`test_release_install_retire/test_release_assets/test_deployment_recovery/test_backup_recovery/test_release_gates` 合计 59 passed；
- 归因实证（平台词汇表回归）：在隔离 worktree 临时回退 `manager.py` 一行 → `test_stable_channel_requires_complete_declarations` 9/9 恢复通过 → 确认失败由本卡扩展引起（确定回归，非偶发/非并行在途），随即以理解性适配修复；
- 变异测试：6 个变异体（平台门回退×2、退役门失效、自报密钥放行、部署校验失去 fail-closed、不兼容备份不拒绝）**全部被捕获**，基线 16 绿——`.planning/2026-10-25-aln21a/mutation-results.log`；
- 隔离全量：`.planning/2026-10-25-aln21a/iso-full-server.log`（干净 HEAD + 本卡改动，最终数以日志为准）；
- 实测探针：`python .planning/2026-10-25-aln21a/probe.py` → `windows-terminal-evidence.json`（环境绑定 + 五场景真实结果）。

## 回滚与未完成事项

- 回滚：`git revert` 本卡提交即可——平台词汇表两行还原后，`test_release_signing.py` 的 `["linux"]` 用例仍是不受支持平台（语义不变），验证测试整体删除，无数据迁移需回退。
- 保留缺口（后续/他方）：
  1. **平板终端未验证**（无硬件；本卡按"或"取 Windows，平板属 Android 终端延伸）；
  2. **Android 客户端在 Windows 宿主上的行为未测**（App 需设备/模拟器，不在本卡环境）；
  3. 部署/恢复编排**仍未接入 CLI/API/管理端**（17-P 保留缺口，本卡未越界）；
  4. **身份登记簿不在三库备份范围内**：本卡验证并记录了其边界语义（活信任件、恢复不回滚、丢失显式降级），但是否需要"登记簿随部署清单做完整性基线"属产品决策，留给集成角色；
  5. 生产云主机/多机存储上的部署恢复未验证（本机单实例）；
  6. Windows 终端侧尚无端侧安装台账/TrustedKeyRing 等价物（Android 有 11-P InstallJournal；CLI 宿主不安装制品，暂无此概念）。

## 审阅结论：待审

## 整包结论：仍实现中（ALN-21：Windows 终端部署/恢复/回执/单一权威已实测；平板与生产环境待后续终端）
