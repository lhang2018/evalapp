# ALN-17-A 交付：备份/恢复闭环加固与迁移回退契约

任务：ALN-17-A 备份/恢复闭环加固与迁移回退契约
负责人：zcode-main（2026-10-24 按队列认领开工）
审阅人：待定（须独立复核，不接受自审自收）
基线 commit：`c7726e8`（实际接手锚点；基线仍 candidate，`published_commit` 为空）
分支与最终 commit：main，本卡代码与台账随本交付提交（见 git log "feat(ALN-17-A)"）
认领状态：delivered（review，待独立复核）

## 触发场景与行为变化

- 输入/触发：跨版本恢复（备份来自不同 schema 版本的程序）、恢复前完整性
  把关、迁移失败回退、恢复耗时/损失量化。
- 修改前：manifest 不含 schema 版本，跨版本恢复不拒绝也不升级；恢复不
  核对行数快照；迁移失败无回退保障；恢复无耗时/损失测量。
- 修改后：
  - `create()` 的 manifest 记录 `schema_version`（主库迁移台账的实际版本）。
  - `verify()` 对缺 `schema_version` 的旧格式备份判不通过（明确规则拒绝）。
  - `restore()` 五段式：校验 → **兼容判定**（备份版本高于当前程序支持即
    拒绝并给出可操作原因）→ 安全备份 → **暂存区行数快照核对 + 按迁移
    路径升级**（低于当前版本的备份在暂存副本上执行 `apply_migrations`，
    失败即中止且当前数据未被触碰）→ 原子替换 → **恢复后逐键计数对账**
    （不符则报错并指向安全备份）。结果新增 `schema_version`、
    `migrations_applied`、`duration_ms`、`counts_verified`。
  - `apply_migrations()` 每条迁移独立 SAVEPOINT：失败自动回退到该条
    迁移前状态（更早迁移与业务数据不变、部分变更不残留），抛
    `MigrationError(version, name, cause)`；回退入口写入模块文档
    （执行中失败=SAVEPOINT 自动回退；已提交后发现问题=迁移前备份走
    restore 恢复，演练见 tests/test_migration_rollback.py）。
  - 恢复只还原状态，不执行历史副作用：事件按快照原样还原，计数对账
    保证无额外执行。

## 范围

- 独占新增：`tests/test_backup_recovery.py`（7 项）、
  `tests/test_migration_rollback.py`（4 项）、
  `tools/measure_backup_recovery.py`（RTO/RPO 实测）、本交付文档。
- 卡内小改：`evoagent/core/backup.py`（manifest/verify/restore 如上；
  备份 zip 字节格式不变，仅 manifest 增键）、
  `evoagent/core/schema/migrations.py`（SAVEPOINT 原子化 + MigrationError，
  未新增迁移、未改已发布迁移函数）。
- 契约：提供方 BackupManager/Migrations；消费方 CLI/REST 管理端（接口
  签名不变，返回字段增量）、persistence（apply_migrations 行为向上兼容）。
- 旧格式兼容规则：缺 `schema_version` 的 manifest 在 verify/restore 均
  拒绝，报错信息给出可操作指引；未静默迁移。
- 本卡不改 `evoagent/release/*`、`evoagent/core/events.py`；通知在途
  改动不触碰。

## 验证证据

- 正常路径：v3→v3 恢复往返；v2 备份恢复进 v3 程序
  （`migrations_applied == ["async_approval_bindings"]`，升级后
  `_schema_version()==3`，任务历史可读）。
- 拒绝/失败/重启：高于当前版本拒绝（含"升级程序"指引）；旧格式缺
  schema_version 拒绝；篡改 events.db、缺 tasks 表、行数快照不符三类
  全部取消恢复且当前数据逐键不变；迁移注入失败后版本不变、探针表不
  残留、任务可读可写、修复后再次应用幂等。
- 命令与结果：`python -m pytest tests -q` → **1626 passed / 1 skipped**
  （基线 1602 + 本卡 11 + 并行会话新增）；定向
  `pytest tests/test_backup_recovery.py tests/test_migration_rollback.py
  tests/test_backup.py -q` → 19 passed。
- RTO/RPO 实测（`python tools/measure_backup_recovery.py
  --output .planning/2026-10-24-aln17a/measure-report.json`）：
  负载 5 任务/105 事件/8 记忆/1 发布清单（备份约 1.2MB），空目录恢复
  **RTO ≈ 0.028s**，恢复后与快照逐行一致（**RPO = 0 行**），快照后增量
  1 任务即事故损失暴露窗口。注意：RTO 为开发机小数据集数值，
  生产规模（真实库大小/磁盘）需按 ALN-17 整包验收另测。
- Mock/真实边界：全部为本地 SQLite/App 夹具（mock 模型、无外部网络）；
  空机器部署文档、制品/版本组合恢复、Mini PC/Linux 常驻部署按任务卡
  原范围另行验收。

## 回滚与未完成事项

- 回滚：revert 本卡提交即回到原 restore/apply_migrations 行为；备份 zip
  格式向后兼容（新 manifest 多一键），无数据迁移需要逆转。
- 未完成（按任务卡原范围保留）：版本资产组合恢复（ALN-03/06/16 关闭
  部分）、空机器部署与部署文档、生产规模 RTO/RPO 与真实故障演练、
  恢复后进程内缓存重载自动化（当前按既有约定建议重启）。
- 影响主题：S-12（备份/迁移/灾难恢复）、Arch 20.4；ALN-17 整包仍开放。

审阅结论：待审
整包结论：仍实现中
