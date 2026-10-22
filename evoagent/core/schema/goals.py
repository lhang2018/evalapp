"""Goal域持久表（09卷§7）；新表自动发现，旧Task保留查询兼容。"""

DOMAIN = "goals"

TABLES = [
    """CREATE TABLE IF NOT EXISTS goals (goal_id TEXT PRIMARY KEY,
        parent_goal_id TEXT, owner TEXT NOT NULL, status TEXT NOT NULL,
        revision INTEGER NOT NULL, deadline REAL, data TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_goals_parent ON goals(parent_goal_id)",
    """CREATE TABLE IF NOT EXISTS goal_grants (authorization_ref TEXT PRIMARY KEY,
        goal_id TEXT NOT NULL, data TEXT NOT NULL, revoked_at REAL)""",
    """CREATE TABLE IF NOT EXISTS goal_tasks (task_id TEXT PRIMARY KEY,
        goal_id TEXT NOT NULL, action TEXT NOT NULL, refs TEXT NOT NULL,
        budget_policy TEXT NOT NULL, task_policy TEXT)""",
    """CREATE TABLE IF NOT EXISTS goal_evidence (goal_id TEXT NOT NULL,
        revision INTEGER NOT NULL, event_type TEXT NOT NULL, actor TEXT NOT NULL,
        reason TEXT NOT NULL, created_at REAL NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY(goal_id, revision))""",
    """CREATE TABLE IF NOT EXISTS goal_commands (scope TEXT NOT NULL,
        command_id TEXT NOT NULL, fingerprint TEXT NOT NULL, result TEXT NOT NULL,
        PRIMARY KEY(scope, command_id))""",
]

PLANNED_TABLES = ()
