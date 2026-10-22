"""能力资产域表（契约 §6 / F-11 / ALN-06-A）。

与任务、评测、路由表分开：资产是**按精确版本冻结的内容**，
生命周期迁移需要独立可追溯台账；混进任务表会让“在用版本”不可判定。
"""

DOMAIN = "assets"

TABLES = [
    """CREATE TABLE IF NOT EXISTS assets(
        asset_id TEXT NOT NULL,
        version TEXT NOT NULL,
        asset_type TEXT NOT NULL,
        namespace TEXT NOT NULL,
        lifecycle_state TEXT NOT NULL,
        content_digest TEXT NOT NULL,
        content_json TEXT NOT NULL,
        source_refs_json TEXT NOT NULL,
        dependency_refs_json TEXT NOT NULL,
        compatibility_json TEXT NOT NULL,
        evaluation_evidence_refs_json TEXT NOT NULL,
        rollback_ref TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (asset_id, version)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_assets_type_state
        ON assets(asset_type, lifecycle_state)""",
    """CREATE TABLE IF NOT EXISTS asset_events(
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_id TEXT NOT NULL,
        version TEXT NOT NULL,
        action TEXT NOT NULL,
        from_state TEXT,
        to_state TEXT,
        detail_json TEXT NOT NULL,
        created_at REAL NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_asset_events_ref
        ON asset_events(asset_id, version, created_at)""",
]
