"""tools 域建表语句（工具执行幂等日志与预算账本）。

对应 PRD 14-16 与 PRD 55-58。
"""

DOMAIN = "tools"

TABLES = [
    """CREATE TABLE IF NOT EXISTS tool_executions(
        idem_key TEXT PRIMARY KEY, tool_id TEXT, created_at REAL,
        result TEXT)""",
    # 日/月预算账本（Arch 12）：scope:period 为键
    """CREATE TABLE IF NOT EXISTS budget_ledger(
        ledger_key TEXT PRIMARY KEY, model_calls INTEGER DEFAULT 0,
        tool_calls INTEGER DEFAULT 0, cost REAL DEFAULT 0,
        seconds REAL DEFAULT 0, updated_at REAL)""",
]