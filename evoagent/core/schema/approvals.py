"""审批记录（契约 09 卷 §1 统一标识 approval_id + A-01，F-04）。

五项绑定：动作摘要 / 目标对象 / 范围 / 有效期 / 一次性语义——
见 evoagent/core/approvals.py。stmt 幂等。

⚠️ approval_id 在本表落地之前**全仓零命中**（GAP：无任何途径能为其产生值）。
"""

DOMAIN = "approvals"

TABLES = [
    """CREATE TABLE IF NOT EXISTS approvals (
  approval_id TEXT PRIMARY KEY,
  task_id TEXT,
  op TEXT NOT NULL,
  target TEXT,
  scope TEXT,
  action_summary TEXT NOT NULL,
  status TEXT NOT NULL,
  granted_by TEXT,
  created_at REAL NOT NULL,
  expires_at REAL,
  consumed_at REAL,
  revoked_at REAL,
  action_digest TEXT,
  binding_json TEXT,
  requested_run_id TEXT
)""",
    "CREATE INDEX IF NOT EXISTS "
    "idx_approvals_task ON approvals(task_id)",
    "CREATE INDEX IF NOT EXISTS "
    "idx_approvals_status ON approvals(status)",
]
