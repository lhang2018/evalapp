"""Runtime-owned memory bindings, grants and context provenance."""
DOMAIN = "memory_context"
TABLES = [
    """CREATE TABLE IF NOT EXISTS task_memory_bindings(
        task_id TEXT PRIMARY KEY, binding_ref TEXT UNIQUE NOT NULL,
        principal_id TEXT NOT NULL, data TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS memory_access_grants(
        grant_ref TEXT PRIMARY KEY, principal_id TEXT NOT NULL,
        expires_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS context_manifests(
        task_id TEXT NOT NULL, run_id TEXT NOT NULL, manifest_id TEXT NOT NULL,
        digest TEXT NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY(task_id,run_id,manifest_id))""",
]
