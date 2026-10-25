"""价格档案 / 结算对账 / 代表性任务集域建表语句（ALN-08-P）。

只放本卡新增的三张表：
- `model_prices`：版本化供应商价格档案（来源、有效期、币种、单价）；
- `task_sets`：带来源与脱敏声明的代表性任务集（整体 JSON 快照）。

建表语句幂等，走 `core/schema` 域模块约定，不写版本化迁移。
"""

DOMAIN = "cost_catalog"

TABLES = [
    """CREATE TABLE IF NOT EXISTS model_prices (
        price_id TEXT PRIMARY KEY, model_id TEXT NOT NULL,
        currency TEXT NOT NULL, source_ref TEXT NOT NULL,
        effective_from REAL NOT NULL, effective_to REAL, data TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS idx_model_prices_model
        ON model_prices(model_id, effective_from)""",
    """CREATE TABLE IF NOT EXISTS task_sets (
        set_id TEXT NOT NULL, version TEXT NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY(set_id, version))""",
]
