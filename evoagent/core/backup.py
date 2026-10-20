"""数据备份与恢复（Arch 20.4）。

把事务数据（任务/发布/审计）、事件与记忆三个 SQLite 库打包为 zip，
附校验和、完整性检查与行数快照。恢复前必须通过校验；在线恢复先做
安全备份，避免中途失败丢失当前状态。恢复后需重启进程以重载内存缓存。
"""

import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_FILES = ("evoagent.db", "events.db", "memory.db")
REQUIRED_TABLES = {
    "evoagent.db": "tasks",
    "events.db": "runtime_events",
    "memory.db": "memory_items",
}
BACKUP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.zip$")
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _integrity_check(path: Path) -> bool:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and row[0] == "ok"
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


def _tables(path: Path) -> set:
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {r[0] for r in rows}
    except sqlite3.DatabaseError:
        return set()
    finally:
        conn.close()


class BackupManager:
    def __init__(self, data_dir: str, *, db=None, bus=None, memory=None,
                 releases=None):
        self.data_dir = Path(data_dir)
        self.db = db
        self.bus = bus
        self.memory = memory
        self.releases = releases

    @property
    def backup_dir(self) -> Path:
        path = self.data_dir / "backups"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _components(self):
        return (("evoagent.db", self.db), ("events.db", self.bus),
                ("memory.db", self.memory))

    def _count(self, comp, table: str) -> int:
        if comp is None:
            return 0
        try:
            return int(comp._conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except sqlite3.DatabaseError:
            return 0

    def _counts(self) -> Dict[str, int]:
        return {
            "tasks": self._count(self.db, "tasks"),
            "events": self._count(self.bus, "runtime_events"),
            "memories": self._count(self.memory, "memory_items"),
            "release_manifests": self._count(self.db, "release_manifests"),
        }

    def _resolve(self, ref: str) -> Path:
        """API 只传文件名；CLI 允许传已有路径。"""
        raw = (ref or "").strip()
        if not raw:
            raise ValueError("缺少备份文件名")
        direct = Path(raw)
        if direct.is_absolute():
            return direct
        if not BACKUP_RE.match(raw) or "/" in raw or "\\" in raw:
            raise ValueError("非法备份文件名")
        return self.backup_dir / raw

    def create(self, name: Optional[str] = None) -> Dict[str, Any]:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        name = name or f"backup_{stamp}_{uuid.uuid4().hex[:6]}.zip"
        if not BACKUP_RE.match(name):
            raise ValueError("非法备份文件名")
        path = self.backup_dir / name
        if path.exists():
            raise ValueError("备份文件已存在")
        files: Dict[str, Dict[str, Any]] = {}
        with tempfile.TemporaryDirectory() as td:
            staged = []
            for dbfile, comp in self._components():
                tmp = Path(td) / dbfile
                if comp is not None:
                    dest = sqlite3.connect(str(tmp))
                    try:
                        comp._conn.backup(dest)
                    finally:
                        dest.close()
                else:
                    src = self.data_dir / dbfile
                    if not src.exists():
                        continue
                    shutil.copyfile(src, tmp)
                files[dbfile] = {"sha256": _sha256_file(tmp),
                                 "bytes": tmp.stat().st_size}
                staged.append((dbfile, tmp))
            manifest = {
                "format": FORMAT_VERSION,
                "created_at": time.time(),
                "files": files,
                "counts": self._counts(),
                "release_id": (self.releases.current_release_id
                               if self.releases else None),
                "restore_note": "恢复后需重启进程以重载内存缓存",
            }
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
                for dbfile, tmp in staged:
                    zf.write(tmp, dbfile)
                zf.writestr(MANIFEST_NAME,
                            json.dumps(manifest, ensure_ascii=False))
        return {"name": name, "path": str(path),
                "created_at": manifest["created_at"],
                "files": files, "counts": manifest["counts"]}

    def verify(self, ref: str) -> Dict[str, Any]:
        path = self._resolve(ref)
        if not path.exists():
            raise FileNotFoundError(f"备份不存在: {ref}")
        errors: List[str] = []
        manifest: Dict[str, Any] = {}
        try:
            with zipfile.ZipFile(path) as zf:
                names = set(zf.namelist())
                if MANIFEST_NAME not in names:
                    raise ValueError("备份缺少 manifest.json")
                manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
                if manifest.get("format") != FORMAT_VERSION:
                    errors.append("备份格式版本不兼容")
                with tempfile.TemporaryDirectory() as td:
                    for dbfile in DB_FILES:
                        if dbfile not in names:
                            errors.append(f"缺少数据库文件: {dbfile}")
                            continue
                        expected = (manifest.get("files") or {}).get(
                            dbfile, {}).get("sha256")
                        if not expected:
                            errors.append(f"清单缺少校验和: {dbfile}")
                            continue
                        tmp = Path(td) / dbfile
                        with zf.open(dbfile) as src, open(tmp, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        if _sha256_file(tmp) != expected:
                            errors.append(f"校验和不匹配: {dbfile}")
                            continue
                        if not _integrity_check(tmp):
                            errors.append(f"SQLite 完整性检查失败: {dbfile}")
                            continue
                        table = REQUIRED_TABLES[dbfile]
                        if table not in _tables(tmp):
                            errors.append(f"缺少必需表 {table}: {dbfile}")
        except (zipfile.BadZipFile, ValueError, OSError) as e:
            errors.append(str(e))
        return {"ok": not errors, "errors": errors, "path": str(path),
                "manifest": manifest}

    def restore(self, ref: str, *, safety_backup: bool = True) -> Dict[str, Any]:
        check = self.verify(ref)
        if not check["ok"]:
            raise ValueError("备份校验失败: " + "; ".join(check["errors"]))
        path = Path(check["path"])
        safety = None
        if safety_backup and self.db is not None:
            safety = self.create(
                f"pre_restore_{time.strftime('%Y%m%d-%H%M%S')}"
                f"_{uuid.uuid4().hex[:4]}.zip")["name"]
        restored: List[str] = []
        with zipfile.ZipFile(path) as zf, tempfile.TemporaryDirectory() as td:
            for dbfile, comp in self._components():
                if dbfile not in check["manifest"].get("files", {}):
                    continue
                tmp = Path(td) / dbfile
                with zf.open(dbfile) as src, open(tmp, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                if comp is not None:
                    src_conn = sqlite3.connect(str(tmp))
                    try:
                        src_conn.backup(comp._conn)
                    finally:
                        src_conn.close()
                else:
                    shutil.copyfile(tmp, self.data_dir / dbfile)
                restored.append(dbfile)
        return {"ok": True, "name": path.name, "restored": restored,
                "safety_backup": safety, "counts": self._counts(),
                "restart_recommended": True}

    def list(self) -> List[Dict[str, Any]]:
        out = []
        for p in sorted(self.backup_dir.glob("*.zip"),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            entry: Dict[str, Any] = {
                "name": p.name, "path": str(p), "bytes": p.stat().st_size,
                "created_at": p.stat().st_mtime}
            try:
                with zipfile.ZipFile(p) as zf:
                    manifest = json.loads(
                        zf.read(MANIFEST_NAME).decode("utf-8"))
                entry["counts"] = manifest.get("counts")
                entry["release_id"] = manifest.get("release_id")
            except (KeyError, ValueError, OSError, zipfile.BadZipFile):
                entry["error"] = "清单不可读"
            out.append(entry)
        return out
