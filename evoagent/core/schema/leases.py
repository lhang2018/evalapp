"""Execution ownership is additive; generation is retained after release."""
DOMAIN = "run_leases"
TABLES = ["""CREATE TABLE IF NOT EXISTS run_leases(
    task_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, owner_id TEXT NOT NULL,
    generation INTEGER NOT NULL, expires_at REAL NOT NULL, active INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS execution_budget_state(
    task_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, state TEXT NOT NULL)"""]
