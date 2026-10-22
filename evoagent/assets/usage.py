"""Read existing frozen references before collecting a retired asset version.

No writes, lock acquisition or commits: callers performing collection already
hold the shared database lock. Historical releases and runs retain their pins.
"""
import json

from .entity import AssetValidationError


def persisted_references(conn, ref):
    sources = (("release_manifests", "release_id", "data"),
               ("runtime_manifests", "manifest_id", "data"),
               ("tasks", "task_id", "data"),
               ("task_runs", "run_id", "version_manifest"))

    def uses(value):
        if isinstance(value, dict):
            refs = value.get("asset_refs", [])
            if isinstance(refs, list) and any(isinstance(item, dict) and
                    item.get("asset_id") == ref.asset_id and item.get("version") == ref.version
                    for item in refs):
                return True
            return any(uses(v) for v in value.values() if isinstance(v, (dict, list)))
        if isinstance(value, list):
            return any(uses(item) for item in value)
        return False

    references = []
    for table, identity, column in sources:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            continue
        # Identifiers are fixed above; no request text enters SQL identifiers.
        for row in conn.execute(f"SELECT {identity}, {column} FROM {table}"):
            try:
                value = json.loads(row[column] or "{}")
            except (TypeError, ValueError) as exc:
                raise AssetValidationError(f"无法核验 {table} 冻结引用，拒绝回收") from exc
            if uses(value):
                references.append(f"{table}:{row[identity]}")
    return references
