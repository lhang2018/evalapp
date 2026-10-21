"""diagnostics 域建表语句（诊断案例/假设/实验）。

对应 PRD 41-45 与 Architecture 9.2-9.3。
"""

DOMAIN = "diagnostics"

TABLES = [
    """CREATE TABLE IF NOT EXISTS diagnostic_cases(
        case_id TEXT PRIMARY KEY, task_id TEXT, run_id TEXT, status TEXT,
        created_at REAL, data TEXT)""",
    """CREATE INDEX IF NOT EXISTS idx_diagnostic_cases_task
        ON diagnostic_cases(task_id)""",
    """CREATE TABLE IF NOT EXISTS diagnostic_hypotheses(
        hypothesis_id TEXT PRIMARY KEY, case_id TEXT, category TEXT,
        confidence REAL, status TEXT, data TEXT)""",
    """CREATE INDEX IF NOT EXISTS idx_diagnostic_hypotheses_case
        ON diagnostic_hypotheses(case_id)""",
    """CREATE TABLE IF NOT EXISTS diagnostic_experiments(
        experiment_id TEXT PRIMARY KEY, case_id TEXT, hypothesis_id TEXT,
        method TEXT, result TEXT, cost REAL, started_at REAL,
        finished_at REAL, data TEXT)""",
    """CREATE INDEX IF NOT EXISTS idx_diagnostic_experiments_case
        ON diagnostic_experiments(case_id)""",
]