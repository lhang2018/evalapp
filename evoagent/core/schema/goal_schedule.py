"""Goal 周期调度域建表语句（ALN-13-P）。

`goals.py` 承载 Goal 实体/授权/证据，`goal_governance.py` 承载 14-A 的策略/
控制/观察；本域只放「周期推进」的调度状态与租约，供
`evoagent/goals/scheduler.py` 使用。

列语义：
- `next_due_at`：下次允许推进的时刻（持久，重启后按原计划继续）；
- `lease_owner/lease_token/lease_expires_at`：多进程领取租约，fencing 见
  scheduler（`attempts` 每次接管 +1，旧 token 失效）；
- `consecutive_failures`：连续失败计数，用于指数退避；
- `last_outcome`：上一轮结果（JSON），供守护与排障读取。
"""

DOMAIN = "goal_schedule"

TABLES = [
    """CREATE TABLE IF NOT EXISTS goal_schedules (
        goal_id TEXT PRIMARY KEY, next_due_at REAL NOT NULL,
        interval_seconds REAL NOT NULL, lease_owner TEXT, lease_token TEXT,
        lease_expires_at REAL, attempts INTEGER NOT NULL DEFAULT 0,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        last_outcome TEXT, last_run_at REAL, data TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS idx_goal_schedules_due
        ON goal_schedules(next_due_at)""",
]
