"""ALN-07-C 增量域：WAITING_USER(reconciliation) 人工裁决记录。

幂等建表（每次连接重放），不修改既有表；裁决只作证据，不触发重放。
"""

DOMAIN = "reconciliation_decisions"

TABLES = [
    """CREATE TABLE IF NOT EXISTS reconciliation_decisions (
        task_id TEXT NOT NULL,
        target_id TEXT NOT NULL,
        target_kind TEXT NOT NULL,
        decision TEXT NOT NULL,
        actor TEXT NOT NULL,
        reason TEXT NOT NULL,
        created_at REAL NOT NULL,
        data TEXT NOT NULL,
        PRIMARY KEY(task_id, target_id))""",
    """CREATE INDEX IF NOT EXISTS idx_reconciliation_decisions_task
        ON reconciliation_decisions(task_id)""",
]
