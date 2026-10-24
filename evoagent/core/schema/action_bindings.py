"""运行时动作绑定（ALN-07-B）。域标识 action_bindings。

记录「这次副作用是由计划哪一步触发的」这一条证据，供 ALN-07-E 的对账作为
`step_bindings` 入参使用。**它不是授权凭据**：绑定存在不代表动作被允许，
也不改变任何工具可见性。

`effect_id` 作为主键，使「一个副作用只能归属一个步骤」成为**数据库层面的
硬约束**，而不是仅靠应用层判断。stmt 幂等：每次连接重放。
"""

DOMAIN = "action_bindings"

TABLES = [
    """CREATE TABLE IF NOT EXISTS action_step_bindings (
  effect_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  tool_id TEXT,
  bound_at REAL NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS "
    "idx_action_bindings_task ON action_step_bindings(task_id)",
    "CREATE INDEX IF NOT EXISTS "
    "idx_action_bindings_step ON action_step_bindings(task_id, step_id)",
]
