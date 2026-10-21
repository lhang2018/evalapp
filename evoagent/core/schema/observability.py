"""observability 域建表语句（四层日志中的审计层、执行轨迹层与诊断证据层）。

对应 PRD 37-40 / 84-86 与 Architecture 9.2 / 18。
"""

DOMAIN = "observability"

TABLES = [
    # 安全控制面审计（Arch 18）
    """CREATE TABLE IF NOT EXISTS audit_log(
        audit_id TEXT PRIMARY KEY, at REAL, actor TEXT, action TEXT,
        target TEXT, outcome TEXT, detail TEXT)""",
    """CREATE INDEX IF NOT EXISTS idx_audit_log_at
        ON audit_log(at)""",
    # 执行轨迹层（Arch 9.2，保留期短）
    """CREATE TABLE IF NOT EXISTS execution_traces(
        trace_id TEXT PRIMARY KEY, task_id TEXT, run_id TEXT, step INTEGER,
        at REAL, kind TEXT, summary TEXT, payload TEXT,
        privacy_classification TEXT)""",
    """CREATE INDEX IF NOT EXISTS idx_execution_traces_task
        ON execution_traces(task_id, step)""",
    """CREATE INDEX IF NOT EXISTS idx_execution_traces_at
        ON execution_traces(at)""",
    # 诊断证据层（Arch 9.2，导出需显式授权）
    """CREATE TABLE IF NOT EXISTS diagnostic_evidence(
        evidence_id TEXT PRIMARY KEY, task_id TEXT, kind TEXT, ref TEXT,
        summary TEXT, privacy_classification TEXT, authorized INTEGER,
        created_at REAL)""",
    """CREATE INDEX IF NOT EXISTS idx_diagnostic_evidence_task
        ON diagnostic_evidence(task_id)""",
]