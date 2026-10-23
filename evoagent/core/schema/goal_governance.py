"""Versioned governance policies, objective mappings, observations and candidates."""
DOMAIN = "goal_governance"
TABLES = [
    "CREATE TABLE IF NOT EXISTS goal_policies (ref TEXT PRIMARY KEY, data TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS goal_controls (goal_id TEXT PRIMARY KEY, data TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS goal_observations (task_id TEXT PRIMARY KEY, goal_id TEXT NOT NULL, data TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_goal_observations_goal ON goal_observations(goal_id)",
    "CREATE TABLE IF NOT EXISTS goal_opportunities (candidate_id TEXT PRIMARY KEY, source_ref TEXT UNIQUE NOT NULL, data TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS goal_advances (goal_id TEXT NOT NULL, command_id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(goal_id,command_id))",
]
PLANNED_TABLES = ()
