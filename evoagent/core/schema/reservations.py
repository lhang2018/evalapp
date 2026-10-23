"""ALN-15 additive domain tables; existing budget usage rows stay compatible."""
DOMAIN = "budget_reservations"

TABLES = [
    """CREATE TABLE IF NOT EXISTS budget_runtime_pool_baselines (
        account_id TEXT PRIMARY KEY, data TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS budget_runtime_bindings (
        task_id TEXT PRIMARY KEY, data TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS budget_accounts (
        account_id TEXT PRIMARY KEY, parent_id TEXT, kind TEXT NOT NULL,
        reference TEXT NOT NULL, currency TEXT NOT NULL, limits TEXT NOT NULL,
        created_at REAL NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS budget_reservations (
        reservation_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
        idem_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
        state TEXT NOT NULL, dispatched INTEGER NOT NULL,
        expires_at REAL NOT NULL, data TEXT NOT NULL,
        UNIQUE(account_id, idem_key))""",
    """CREATE INDEX IF NOT EXISTS idx_budget_reservations_expiry
        ON budget_reservations(state, expires_at)""",
    """CREATE TABLE IF NOT EXISTS budget_reservation_accounts (
        reservation_id TEXT NOT NULL, account_id TEXT NOT NULL,
        PRIMARY KEY(reservation_id, account_id))""",
    """CREATE INDEX IF NOT EXISTS idx_budget_reservation_accounts_account
        ON budget_reservation_accounts(account_id)""",
    """CREATE TABLE IF NOT EXISTS budget_reservation_events (
        reservation_id TEXT NOT NULL, revision INTEGER NOT NULL,
        event TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
        created_at REAL NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY(reservation_id, revision))""",
]
