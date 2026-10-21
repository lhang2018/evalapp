"""memory 域建表语句（记忆质量评测报告）。

记忆正文的表在 `memory/store.py` 中自建（它有自己的连接与生命周期），
这里只放运行侧的记忆评测报告（PRD 28）。
"""

DOMAIN = "memory"

TABLES = [
    """CREATE TABLE IF NOT EXISTS memory_eval_reports(
        report_id TEXT PRIMARY KEY, dataset_id TEXT, version TEXT,
        created_at REAL, data TEXT)""",
]