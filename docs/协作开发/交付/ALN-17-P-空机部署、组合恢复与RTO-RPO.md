# ALN-17-P 空机部署、组合恢复与RTO/RPO

任务：ALN-17-P / 空机部署、组合恢复与 RTO/RPO
负责人：dsh-lead（本会话认领）
审阅人：待定（未分配独立审阅人，不自批为独立验收）
基线 commit：`26749474e41827b4e775b491ffe00bbe57928e15`（认领时的 `feature/user-direction` HEAD，不是已发布共同基线）
分支与最终 commit：`feature/user-direction` / `b38c5aceb03220bf96ce18d960f9c2b4ebc0a977`（tree `d37d4b9bea2c0f3398a1eae91fb1d881fe1c2621`）
认领状态：claimed → **review**

## 触发场景与行为变化

- 输入/触发：
  - 一台**空机器**要变成可运行的 EvoAgent 实例；
  - 事故后要从固定备份**组合恢复**数据三库 + 部署身份 + 行数快照；
  - 恢复要用真实时间与损失窗口回答「RTO/RPO 是多少」，以及备份不兼容时能否拒绝、失败时能否回退。
- 修改前：
  - `core/backup.py`（ALN-17-A 已接受）已能打包三库、校验、恢复（含安全备份、schema 升级、行数对账），但**没有空机部署**（没有干净环境初始化与部署身份基线），也没有**组合恢复编排**（兼容判定前置、身份保持、RTO/RPO 与损失窗口记录、失败回退实测）。
- 修改后（新增 `evoagent/deployment/`、`evoagent/recovery/` 两个包；**未改** `core/backup.py` 一行）：
  - `deployment.bootstrap`：空目录校验（有残留即拒绝覆盖）→ 初始化 `evoagent.db`/`events.db`/`memory.db` + `backups/`/`logs/` 布局 → 写 `deployment.json`（实例 ID、部署时间、源码版本、各库 schema 版本、部署时哈希基线）→ `verify` 只核对**结构与版本**（主库迁移版本、必需表、目录、清单），数据哈希漂移仅作为审计信息，不把可变数据当不可变。
  - `recovery.CompositeRecovery`：恢复前先 `inspect`（备份校验 + **schema 兼容判定**：备份版本高于当前程序 → `IncompatibleBackup` 拒绝，且**不动数据**；低于则按迁移路径升级并在报告记录）；组合恢复时先做安全备份 → 真实恢复 → 行数快照与**实例身份**对账（身份不被备份内容覆盖）；记录**真实 RTO**（恢复墙钟）与 **RPO/损失窗口**（事故时刻 − 备份创建时刻），并与配置的验收界比对（明确「目标值不是生产 SLA」）。
  - `restore_with_rollback`：恢复失败时自动从安全备份回退，记录回退耗时与「回退后行数是否复原」；`matrix()` 一次跑出三场景：兼容恢复 / 不兼容拒绝 / 受控故障回退。
  - `forge_incompatible_backup`：构造「schema 高于程序」的高风险备份用于拒绝路径验证。

## 实测结果（本机真实执行，非夹具）

| 场景 | 结果 |
|---|---|
| 空机部署 | 空目录 → 三库+布局+`deployment.json`；`verify.ok=true`；重复部署/非空目录拒绝 |
| 兼容恢复（组合） | 成功；行数与备份快照一致（tasks/events/memories/release_manifests）、实例身份保持；**RTO 0.025s、RPO 0.086s**、损失窗口 from_backup_at→incident |
| 不兼容拒绝 | 构造 `schema_version=SCHEMA_VERSION+5` 的备份 → `IncompatibleBackup`，**恢复前拒绝且目标数据未变**（耗时只在判定阶段） |
| 受控故障回退 | 注入「恢复中途失败」→ 自动回退安全备份，**回退计数与失败前行数一致**、回退耗时记录；实例身份保持 |
| 证据文件 | `.planning/2026-10-25-aln17p/rto-rpo-evidence.json`（含三场景矩阵与真实时间戳） |

## 范围

- 独占模块：`evoagent/deployment/{__init__,bootstrap}.py`、`evoagent/recovery/{__init__,combined}.py`（新增）。
- 共享文件补丁：**无**（本卡 `shared_files` 为空；未改 `core/backup.py`、`app.py`、`api/`、`cli.py`）。
- 新增/修改契约：`deployment.json` 部署清单（格式 v1，含实例身份/schema 基线）、`RecoveryPolicy(rto_target_s, rpo_target_s, take_safety_backup)`、组合恢复报告结构（RTO/RPO/损失窗口/身份/行数/回退）；备份 zip 格式沿用 ALN-17-A（`manifest.schema_version/counts`）。
- 数据迁移/兼容：不新增表、不写迁移；备份兼容性判定直接消费既有 `manifest.schema_version` 与 `SCHEMA_VERSION`。

## 验证证据

- 正常路径：空机部署 → 造数据 → 备份 → 增量变更 → 组合恢复 → 行数回到备份点、身份保持、RTO/RPO 与损失窗口记录（见上表与证据 JSON）。
- 拒绝/失败/回退：
  - 非空目录部署拒绝；缺库文件、主库迁移版本漂移 → `verify.ok=false` 且错误可读；
  - 版本更高的备份 → 拒绝且数据零变化（测试断言 counts 前后一致）；
  - 受控故障注入 → 回退成功、行数复原、回退耗时与错误记录。
- 命令、结果、日志与对应源码 commit：
  - 提交 `b38c5aceb03220bf96ce18d960f9c2b4ebc0a977`；
  - 本卡专项：`python -m pytest tests/test_deployment_recovery.py -q` → **8 passed**；
  - 备份/恢复相关域回归（backup/backup_recovery + 本卡）→ **27 passed**；
  - 实测命令：`python -m evoagent.recovery.combined --out .planning/2026-10-25-aln17p/rto-rpo-evidence.json`；
  - 全量后端：`python -m pytest tests/ -q` → **1942 passed / 5 failed / 1 skipped（391.88s）**，日志 `.planning/2026-10-25-aln17p/backend-tests.txt`。
  - **5 项失败均为他人 ALN-11-P 在途工作**：全部在 `tests/test_release_install_retire.py`（未跟踪的新文件），同时 `evoagent/release/{delivery,manager}.py` 存在他人未提交改动；本卡未改 release 域任何文件，也与 `deployment`/`recovery` 无调用关系（结构性独立）。
- Mock / 本地组件 / 真机 / 真实供应商的验证边界：
  - 本轮为**本机真实文件系统/SQLite** 部署与恢复实测（真实备份 zip、真实行数、真实墙钟），不是生产服务器、不是空机云主机、不是多机备份存储；
  - 备份数据来自本机测试实例（含少量探针任务/事件），不是生产数据；
  - 未消费 ALN-11-P（在途）的生产签名/安装退役回执：本卡的部署/恢复只覆盖数据三库与实例身份，签名制品准入与恢复属 11-P/整包范围；
  - RTO/RPO 目标值为本卡配置的验收界（300s/600s），不构成生产 SLA。

## 回滚与未完成事项

- 回滚触发、操作及验证：`git revert b38c5aceb03220bf96ce18d960f9c2b4ebc0a977`（删两个新包与测试；`deployment.json` 与 backups 为运行期产物、不回滚数据）。回滚后 `tests/test_deployment_recovery.py` 整体消失，`core/backup.py` 既有行为不变。
- 保留的限制、后续 ALN 和依赖：
  - 未在生产云主机/多机对象存储上验证部署与恢复；未做备份存储异地/加密/保留策略；
  - 未消费 ALN-11-P 的签名与安装退役回执（依赖卡在途），组合恢复暂不含签名制品准入校验；
  - RTO/RPO 只测小数据量（单实例探针数据），未测大数据量/冷磁盘/并发恢复；
  - 未把部署/恢复编排接入 CLI/API/管理端（不在本卡写入边界），接入属集成角色。
- 影响的主题/实体/ADR/契约/系统验收条目：新增部署清单与组合恢复报告契约、`RecoveryPolicy` 验收界；备份 zip 格式与恢复语义不变；17-P 完成后 21-A 的三张前置只差 `12-E`。

## 审阅结论：待审

## 整包结论：仍实现中（ALN-17：生产空机环境、真实备份存储与签名制品恢复仍待验；本卡交付空机部署、组合恢复与 RTO/RPO 实测）
