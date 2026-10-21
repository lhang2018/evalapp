"""release 域建表语句（发布清单与发布事件）。

对应 PRD 59-63 与 Architecture 16。
"""

DOMAIN = "release"

TABLES = [
    """CREATE TABLE IF NOT EXISTS release_manifests(
        release_id TEXT PRIMARY KEY, version TEXT, created_at REAL,
        data TEXT)""",
    """CREATE TABLE IF NOT EXISTS release_events(
        event_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT,
        release_id TEXT, created_at REAL, data TEXT)""",
    """CREATE INDEX IF NOT EXISTS idx_release_events_release
        ON release_events(release_id)""",
]