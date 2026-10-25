"""用户反馈与运行证据关联（ALN-29-C）。域标识 feedback。

用户反馈是"报告的现象"，不是根因：cause_status 恒为 unverified，直到
诊断链路给出已验证结论。聚合键（agg_key）让重复反馈合并计数而不是刷屏。
stmt 幂等：每次连接重放。
"""

DOMAIN = "feedback"

TABLES = [
    """CREATE TABLE IF NOT EXISTS user_feedback (
  feedback_id TEXT PRIMARY KEY,
  agg_key TEXT NOT NULL,
  category TEXT NOT NULL,
  text TEXT NOT NULL,
  task_id TEXT,
  run_id TEXT,
  manifest_id TEXT,
  evidence_refs TEXT NOT NULL DEFAULT '[]',
  cause_status TEXT NOT NULL DEFAULT 'unverified',
  count INTEGER NOT NULL DEFAULT 1,
  first_seen_at REAL NOT NULL,
  last_seen_at REAL NOT NULL
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS "
    "idx_feedback_agg ON user_feedback(agg_key)",
    "CREATE INDEX IF NOT EXISTS "
    "idx_feedback_task ON user_feedback(task_id)",
]
