"""路由评测报告表（协作基座 F-00.1 域拆分）。

独立于 QEEG 的评测报告表 —— 路由评测关注**分类决策质量**，
任务评测关注**执行结果**，两者语义不同，混在一张表会让基线对比失真。
"""

DOMAIN = "routing"

TABLES = [
    """CREATE TABLE IF NOT EXISTS routing_reports(
        dataset_id TEXT NOT NULL,
        version TEXT NOT NULL,
        created_at REAL NOT NULL,
        report_json TEXT NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_routing_reports_created
        ON routing_reports(created_at DESC)""",
]
