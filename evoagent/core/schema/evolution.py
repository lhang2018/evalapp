"""evolution 域建表语句（候选生命周期、评测报告、模型迁移）。

对应 PRD 46-54、PRD 20/28 与 Architecture 10-11。
"""

DOMAIN = "evolution"

TABLES = [
    # 进化轨状态（Arch 10）：候选与发布必须可恢复，不能只存内存
    """CREATE TABLE IF NOT EXISTS evolution_candidates(
        candidate_id TEXT PRIMARY KEY, status TEXT, risk_level TEXT,
        created_at REAL, data TEXT)""",
    """CREATE INDEX IF NOT EXISTS idx_evolution_candidates_status
        ON evolution_candidates(status)""",
    """CREATE TABLE IF NOT EXISTS eval_reports(
        report_id TEXT PRIMARY KEY, dataset_id TEXT, candidate_id TEXT,
        created_at REAL, data TEXT)""",
    # 模型迁移记录（PRD 20 / Arch 7.4）
    """CREATE TABLE IF NOT EXISTS model_migrations(
        migration_id TEXT PRIMARY KEY, old_model_id TEXT,
        new_model_id TEXT, status TEXT, created_at REAL,
        updated_at REAL, data TEXT)""",
    """CREATE INDEX IF NOT EXISTS idx_model_migrations_status
        ON model_migrations(status)""",
]