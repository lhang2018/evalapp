"""Frozen failure reproduction inputs; independent of mutable source Task rows."""
DOMAIN = "regression"
TABLES = ["""CREATE TABLE IF NOT EXISTS regression_assets(
    case_id TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL,
    created_at REAL NOT NULL)"""]
