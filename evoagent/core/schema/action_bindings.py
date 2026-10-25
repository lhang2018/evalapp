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
    # ALN-07-C：预绑定意图（消「副作用已发生、绑定未写」崩溃窗口）。
    # 执行前登记（effect_id NULL=未决），执行后回填固化；崩溃重启后由
    # heal_pending 与账本做确定性补绑。它同样不是授权凭据。
    """CREATE TABLE IF NOT EXISTS action_binding_intents (
  intent_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  tool_id TEXT NOT NULL,
  run_id TEXT,
  created_at REAL NOT NULL,
  effect_id TEXT
)""",
    "CREATE INDEX IF NOT EXISTS "
    "idx_binding_intents_task ON action_binding_intents(task_id, effect_id)",
]
