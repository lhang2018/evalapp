"""tasks 域建表语句（Task of Record 与其运行/检查点）。"""

DOMAIN = "tasks"

TABLES = [
    """CREATE TABLE IF NOT EXISTS tasks(
        task_id TEXT PRIMARY KEY, status TEXT, intent TEXT, domain TEXT,
        risk_level TEXT, user_goal TEXT, created_at REAL, data TEXT,
        goal_id TEXT)""",
    """CREATE INDEX IF NOT EXISTS idx_tasks_goal
        ON tasks(goal_id)""",
    """CREATE TABLE IF NOT EXISTS task_runs(
        run_id TEXT PRIMARY KEY, task_id TEXT, started_at REAL,
        finished_at REAL, outcome TEXT, version_manifest TEXT,
        parent_run_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS task_checkpoints(
        checkpoint_id TEXT PRIMARY KEY, task_id TEXT,
        created_at REAL, snapshot TEXT)""",
    """CREATE TABLE IF NOT EXISTS workflow_runs(
        workflow_run_id TEXT PRIMARY KEY, workflow_id TEXT, task_id TEXT,
        status TEXT, node_states TEXT, updated_at REAL)""",
]
