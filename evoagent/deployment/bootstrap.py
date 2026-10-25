"""空机部署（ALN-17-P：干净环境部署与身份/版本基线）。

只做「把一台空机器变成可运行的 EvoAgent 实例」这一步，不复制备份/恢复
已接受切片（`core/backup.py`）：

- 目标目录必须为空（空机语义），有残留文件即拒绝覆盖，避免误部署把
  其他实例的数据当成自己的；
- 初始化三个库（`evoagent.db` / `events.db` / `memory.db`）与目录布局，
  记录 schema 版本与部署身份（实例 ID、部署时间、源码版本）；
- `verify` 只核对**结构与版本**（库文件在、表可读、schema 与源码一致、
  部署身份唯一），不把数据哈希当不可变：部署后数据本来就会变化，
  哈希漂移只作为审计信息返回。
"""

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from ..core.events import EventBus
from ..core.persistence import Database
from ..core.schema.migrations import SCHEMA_VERSION, current_version
from ..memory.store import MemoryStore

MANIFEST_NAME = "deployment.json"
FORMAT = 1
DB_FILES = ("evoagent.db", "events.db", "memory.db")
REQUIRED_TABLES = {
    "evoagent.db": "tasks",
    "events.db": "runtime_events",
    "memory.db": "memory_items",
}
LAYOUT = ("backups", "logs")


class DeploymentError(RuntimeError):
    """空机部署拒绝（目录非空/部署损坏/版本不一致）。"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _close_quietly(component: Any) -> None:
    for closer in ("close",):
        method = getattr(component, closer, None)
        if callable(method):
            try:
                method()
                return
            except Exception:  # noqa: BLE001
                pass
    conn = getattr(component, "_conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _db_meta(path: Path) -> Dict[str, Any]:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        tables = [item[0] for item in row]
        return {"schema_version": current_version(conn), "tables": tables}
    finally:
        conn.close()


def bootstrap(data_dir: str, *, app_version: str = "",
              source_commit: str = "", allow_existing: bool = False) -> Dict[str, Any]:
    """在空目录初始化一个可运行实例，返回部署清单。"""
    target = Path(data_dir)
    if target.exists():
        if not target.is_dir():
            raise DeploymentError("部署目标不是目录: " + str(target))
        entries = [item.name for item in target.iterdir()]
        if entries:
            manifest_path = target / MANIFEST_NAME
            if not (allow_existing and manifest_path.is_file() and set(entries) <= (
                    set(DB_FILES) | set(LAYOUT) | {MANIFEST_NAME})):
                raise DeploymentError(
                    "目标目录非空（空机部署拒绝覆盖）: " + ", ".join(sorted(entries)))
    target.mkdir(parents=True, exist_ok=True)
    for name in LAYOUT:
        (target / name).mkdir(exist_ok=True)
    db = Database(str(target / "evoagent.db"))
    bus = EventBus(str(target / "events.db"))
    memory = MemoryStore(str(target / "memory.db"))
    try:
        components = {"evoagent.db": db, "events.db": bus,
                      "memory.db": memory}
        db_meta = {}
        for dbfile in DB_FILES:
            _close_quietly(components[dbfile])
            path = target / dbfile
            meta = _db_meta(path)
            expected = REQUIRED_TABLES[dbfile]
            if expected not in meta["tables"]:
                raise DeploymentError(
                    f"初始化失败: {dbfile} 缺少表 {expected}")
            db_meta[dbfile] = {"schema_version": meta["schema_version"],
                               "sha256_at_deploy": _sha256(path),
                               "bytes_at_deploy": path.stat().st_size}
    finally:
        for component in (db, bus, memory):
            _close_quietly(component)
    manifest = {
        "format": FORMAT,
        "instance_id": "inst_" + uuid.uuid4().hex,
        "deployed_at": time.time(),
        "app_version": app_version,
        "source_commit": source_commit,
        "schema_version_source": SCHEMA_VERSION,
        "layout": list(LAYOUT),
        "databases": db_meta,
        "note": "schema_version 必须与当前源码一致；数据哈希仅作部署审计基线",
    }
    (target / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n")
    return manifest


def load(data_dir: str) -> Dict[str, Any]:
    path = Path(data_dir) / MANIFEST_NAME
    if not path.is_file():
        raise DeploymentError("缺少部署清单: " + str(path))
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise DeploymentError(f"部署清单损坏: {exc}") from exc
    if not isinstance(manifest, dict) or not manifest.get("instance_id"):
        raise DeploymentError("部署清单缺少实例身份")
    return manifest


def verify(data_dir: str) -> Dict[str, Any]:
    """核对部署结构与版本（不把数据哈希当不可变）。"""
    target = Path(data_dir)
    errors: list = []
    changed_since_deploy: list = []
    try:
        manifest = load(str(target))
    except DeploymentError as exc:
        return {"ok": False, "errors": [str(exc)], "manifest": None,
                "changed_since_deploy": []}
    if manifest.get("format") != FORMAT:
        errors.append(f"部署格式版本不支持: {manifest.get('format')}")
    for name in LAYOUT:
        if not (target / name).is_dir():
            errors.append("缺少目录: " + name)
    for dbfile, table in REQUIRED_TABLES.items():
        path = target / dbfile
        if not path.is_file():
            errors.append("缺少数据库文件: " + dbfile)
            continue
        try:
            meta = _db_meta(path)
        except sqlite3.DatabaseError as exc:
            errors.append(f"{dbfile} 不可读: {exc}")
            continue
        if table not in meta["tables"]:
            errors.append(f"{dbfile} 缺少表 {table}")
        # 版本化迁移只由主库承载；events/memory 库按域模块建表，无迁移台账
        if dbfile == "evoagent.db" and meta["schema_version"] != SCHEMA_VERSION:
            errors.append(
                f"{dbfile} schema 版本 {meta['schema_version']} ≠ 源码 {SCHEMA_VERSION}")
        recorded = (manifest.get("databases") or {}).get(dbfile) or {}
        if recorded.get("sha256_at_deploy") and \
                recorded["sha256_at_deploy"] != _sha256(path):
            changed_since_deploy.append(dbfile)
    return {"ok": not errors, "errors": errors, "manifest": manifest,
            "instance_id": manifest.get("instance_id"),
            "deployed_at": manifest.get("deployed_at"),
            "changed_since_deploy": changed_since_deploy,
            "checked_at": time.time()}
