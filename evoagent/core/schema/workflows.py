"""workflows 域建表语句（工作流资产库与从成功任务抽取的候选）。

对应 PRD 13.4 与 Architecture 16。
"""

DOMAIN = "workflows"

TABLES = [
    """CREATE TABLE IF NOT EXISTS workflow_defs(
        workflow_id TEXT, version TEXT, status TEXT, updated_at REAL,
        data TEXT, PRIMARY KEY(workflow_id, version))""",
    # 单次成功不足以固化（Arch 16.2）
    """CREATE TABLE IF NOT EXISTS workflow_candidates(
        candidate_id TEXT PRIMARY KEY, intent TEXT, signature TEXT,
        status TEXT, support INTEGER, sample_count INTEGER,
        created_at REAL, updated_at REAL, data TEXT)""",
    """CREATE INDEX IF NOT EXISTS idx_workflow_candidates_status
        ON workflow_candidates(status)""",
]