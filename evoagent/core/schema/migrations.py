"""Schema 版本化迁移框架（协作基座 F-00.4）。

**动机**：改动已有表的结构时，协作方各自往 `persistence.py::_init_schema`
里塞 `ALTER TABLE`，既冲突又不可追溯。S-12（备份/迁移/灾难恢复）也要求
迁移台账与正式迁移路径，而不是"不兼容就重建"。

**用法**：

    from .migrations import MIGRATIONS, current_version, apply_migrations

    # 新增一条迁移：只追加，不改历史
    MIGRATIONS.append((2, "add_goal_deadline", _m002_add_goal_deadline))

迁移函数签名固定为 ``fn(conn) -> None``，必须在单个事务内完成。
执行记录写入 ``schema_migrations`` 表，同一个库只会执行一次。
每条迁移在独立 SAVEPOINT 内执行：失败自动回退到该条迁移前的状态
（已应用的更早迁移与业务数据保持不变），并抛出 :class:`MigrationError`。

回退入口（ALN-17-A 演练契约）：
- 迁移执行中失败：SAVEPOINT 自动回退，无需人工介入。
- 迁移已提交后才发现问题：用迁移前的
  :meth:`~evoagent.core.backup.BackupManager.create` 备份走
  ``restore`` 恢复（恢复路径本身带行数核对与安全备份），
  演练见 ``tests/test_migration_rollback.py``。

约定：
- 版本号严格递增，**不得修改已发布的迁移函数**（否则同库不同版本行为分叉）。
- 需要新增表时不要写迁移，走 `core/schema/` 的域模块（建表语句本身幂等）。
"""

import sqlite3
from typing import Callable, List, Tuple

# 当前代码期望的 schema 版本。升级迁移时 +1。
SCHEMA_VERSION = 3

Migration = Tuple[int, str, Callable[[sqlite3.Connection], None]]


class MigrationError(RuntimeError):
    """迁移执行失败；目标库已回退到该条迁移前的状态。"""

    def __init__(self, version: int, name: str, cause: BaseException):
        self.version = version
        self.name = name
        self.cause = cause
        super().__init__(f"迁移 {version}({name}) 失败并已回退: {cause}")


def _m001_baseline(conn: sqlite3.Connection) -> None:
    """基线迁移：把历史上散落在 _init_schema 的 ALTER 逻辑正式化。

    `tasks.created_at` 缺失属于不可兼容的旧结构，历史上直接 DROP 重建；
    `tasks.goal_id` 缺失属于可兼容的加列（G-01 目标关联）。
    这两条已在迁移框架建立前长期生效，此处记录为基线以便台账完整。
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)")]
    if cols and "created_at" not in cols:
        conn.execute("DROP TABLE tasks")
    elif cols and "goal_id" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN goal_id TEXT")


def _m002_run_parent(conn: sqlite3.Connection) -> None:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(task_runs)")]
    if cols and "parent_run_id" not in cols:
        conn.execute("ALTER TABLE task_runs ADD COLUMN parent_run_id TEXT")


def _m003_approval_bindings(conn: sqlite3.Connection) -> None:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(approvals)")]
    if cols:
        for name in ("action_digest", "binding_json", "requested_run_id"):
            if name not in cols:
                conn.execute(f"ALTER TABLE approvals ADD COLUMN {name} TEXT")


# 已发布迁移（顺序即执行顺序）
MIGRATIONS: List[Migration] = [
    (1, "baseline", _m001_baseline),
    (2, "independent_run_parent", _m002_run_parent),
    (3, "async_approval_bindings", _m003_approval_bindings),
]


def current_version(conn: sqlite3.Connection) -> int:
    """返回该库已执行的最高迁移版本；无记录时返回 0。"""
    try:
        row = conn.execute(
            "SELECT MAX(version) FROM schema_migrations").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def apply_migrations(conn: sqlite3.Connection) -> List[str]:
    """执行所有尚未应用的迁移，返回本次实际执行的迁移名列表。

    幂等：重复调用不会重复执行已记录的迁移。
    每条迁移在独立 SAVEPOINT 内执行：某条失败时回退到该条之前的状态，
    已应用的更早迁移保留，并抛出 :class:`MigrationError`。
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations(
            version INTEGER PRIMARY KEY, name TEXT, applied_at REAL)""")
    done = current_version(conn)
    applied: List[str] = []
    for version, name, fn in sorted(MIGRATIONS, key=lambda m: m[0]):
        if version <= done:
            continue
        savepoint = f"migration_{version:04d}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            fn(conn)
            conn.execute(
                "INSERT OR REPLACE INTO schema_migrations(version, name, applied_at)"
                " VALUES (?,?,?)", (version, name, _now()))
        except BaseException as exc:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise MigrationError(version, name, exc) from exc
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        applied.append(name)
    return applied


def _now() -> float:
    import time
    return time.time()
