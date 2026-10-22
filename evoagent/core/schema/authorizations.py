"""Immutable task authorization revisions and operation receipts."""
DOMAIN = "authorizations"
TABLES = [
    """CREATE TABLE IF NOT EXISTS action_authorizations (
        authorization_ref TEXT PRIMARY KEY, task_id TEXT NOT NULL,
        policy_json TEXT NOT NULL, actor TEXT NOT NULL, created_at REAL NOT NULL,
        expires_at REAL NOT NULL, revoked_at REAL)""",
    "CREATE INDEX IF NOT EXISTS idx_action_authorizations_task ON action_authorizations(task_id)",
    """CREATE TABLE IF NOT EXISTS effect_receipts (
        effect_id TEXT PRIMARY KEY, args_json TEXT NOT NULL, tool_version TEXT NOT NULL,
        result_json TEXT, receipt_json TEXT, updated_at REAL NOT NULL)""",
]
