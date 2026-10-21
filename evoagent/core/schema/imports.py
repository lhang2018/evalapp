"""imports 域建表语句（离线诊断包受控导入的隔离区与诊断附件）。

对应补充设计 C §6/§8：导入包与附件都是不可信证据，隔离存放，
不与线上事件混用；数据库只存引用/摘要/分块，正文不落库。
"""

DOMAIN = "imports"

TABLES = [
    """CREATE TABLE IF NOT EXISTS diagnostics_imports(
        import_id TEXT PRIMARY KEY, source TEXT, status TEXT,
        file_name TEXT, size_bytes INTEGER, sha256 TEXT UNIQUE,
        format_version TEXT, exported_at REAL, entry_count INTEGER,
        event_count INTEGER, warnings TEXT, actor TEXT,
        imported_at REAL)""",
    """CREATE INDEX IF NOT EXISTS idx_diagnostics_imports_at
        ON diagnostics_imports(imported_at)""",
    """CREATE TABLE IF NOT EXISTS diagnostics_import_events(
        import_id TEXT, event_id TEXT, task_id TEXT, run_id TEXT,
        runtime_manifest_id TEXT, event_type TEXT, timestamp REAL,
        sequence_no INTEGER, severity TEXT, payload TEXT,
        review_status TEXT, conflict_reason TEXT,
        PRIMARY KEY(import_id, event_id))""",
    """CREATE INDEX IF NOT EXISTS idx_import_events_task
        ON diagnostics_import_events(task_id)""",
    # 附件分块（补充 C §6）：原始字节只在内存，库内仅存分块供拼装校验
    """CREATE TABLE IF NOT EXISTS attachments(
        attachment_id TEXT PRIMARY KEY, file_name TEXT,
        content_type TEXT, size_bytes INTEGER, sha256 TEXT,
        chunk_size INTEGER, chunk_count INTEGER, consent INTEGER,
        status TEXT, received_bytes INTEGER DEFAULT 0, actor TEXT,
        created_at REAL, updated_at REAL, completed_at REAL,
        last_error TEXT)""",
    """CREATE TABLE IF NOT EXISTS attachment_chunks(
        attachment_id TEXT, chunk_index INTEGER, size_bytes INTEGER,
        sha256 TEXT, data BLOB, created_at REAL,
        PRIMARY KEY(attachment_id, chunk_index))""",
]