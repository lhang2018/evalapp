"""副作用账本（契约 09 卷 §8，F-03）。域标识 effects。

六态见 evoagent/core/side_effects.py。stmt 幂等：每次连接重放。
"""

DOMAIN = "effects"

TABLES = [
    """CREATE TABLE IF NOT EXISTS task_side_effects (
  effect_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  run_id TEXT,
  tool_id TEXT NOT NULL,
  idempotency_key TEXT,
  effect_summary TEXT,
  status TEXT NOT NULL,
  external_receipt_ref TEXT,
  last_reconciled_at REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS "
    "idx_side_effects_task ON task_side_effects(task_id)",
    "CREATE INDEX IF NOT EXISTS "
    "idx_side_effects_status ON task_side_effects(status)",
]
