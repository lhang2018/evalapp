"""不可变资产仓储（ALN-06-A 步骤 2/3）。

仓储只负责资产域：登记、按精确版本读取、依赖解析、生命周期与引用查询。
共享持久化、运行 Manifest 与 Android 侧接入由整合补丁处理，本模块不碰。

不可变规则：
- 同一 `asset_id` + `version` 一旦登记，`content_digest` 不得变化；
- 重复登记只有在**契约字段与内容摘要完全一致**时幂等；
- 消费者只能按 `AssetRef` 精确读取，仓库不提供“最新版本”解析。
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from ..core.schema.assets import TABLES
from .content import content_digest
from .entity import (Asset, AssetConflict, AssetNotFound, AssetRef,
                     AssetValidationError, DependencyError, LifecycleError,
                     ensure_transition)
from .mapping import REGISTERABLE_STATES, AssetDraft
from .usage import persisted_references

_JSON_FIELDS = ("source_refs_json", "dependency_refs_json",
                "compatibility_json", "evaluation_evidence_refs_json")


class AssetRepository:
    def __init__(self, conn: sqlite3.Connection, *,
                 ensure_schema: bool = True, lock: Any = None) -> None:
        self._conn = conn
        self._lock = lock
        self._conn.row_factory = sqlite3.Row
        if ensure_schema:
            for stmt in TABLES:
                self._conn.execute(stmt)
            self._conn.commit()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """复用共享库的写锁；没有传入锁时退化为无锁（测试/独占连接）。"""
        if self._lock is None:
            yield
            return
        with self._lock:
            yield

    # ---- 登记 ----
    def register(self, *, asset_id: str, asset_type: str, namespace: str,
                 version: str, content: Dict[str, Any],
                 lifecycle_state: str = "draft",
                 source_refs: Sequence[str] = (),
                 dependency_refs: Sequence[str] = (),
                 compatibility: Optional[Dict[str, Any]] = None,
                 evaluation_evidence_refs: Sequence[str] = (),
                 rollback_ref: Optional[str] = None,
                 content_digest_expected: Optional[str] = None) -> Asset:
        if lifecycle_state not in REGISTERABLE_STATES:
            raise LifecycleError(
                f"登记初始状态只能是 {list(REGISTERABLE_STATES)}；"
                f"approved/active 必须走带证据门槛的迁移")
        digest = content_digest(content)
        if content_digest_expected is not None \
                and content_digest_expected != digest:
            raise AssetValidationError(
                f"内容摘要不符：声明 {content_digest_expected} 实际 {digest}")

        now = time.time()
        asset = Asset(
            asset_id=asset_id, asset_type=asset_type,
            namespace=namespace, version=version,
            content_digest=digest,
            source_refs=tuple(source_refs),
            dependency_refs=tuple(dependency_refs),
            compatibility=dict(compatibility or {}),
            lifecycle_state=lifecycle_state,
            evaluation_evidence_refs=tuple(evaluation_evidence_refs),
            rollback_ref=rollback_ref,
            content=json.loads(json.dumps(content, ensure_ascii=False)),
            created_at=now, updated_at=now)
        asset.validate()

        with self._locked():
            existing = self.get(asset_id, version)
            if existing is not None:
                if existing.content_digest == digest and \
                        existing.to_contract_dict() == asset.to_contract_dict():
                    self._event(asset.ref, "register_idempotent", None, None,
                                {"content_digest": digest})
                    self._conn.commit()
                    return existing
                raise AssetConflict(
                    f"{asset.ref} 已登记且内容/元数据不同 "
                    f"（库 {existing.content_digest} / 新 {digest}）—— "
                    f"同一版本禁止覆盖；请登记新版本")

            # 已回收版本在事件账本里留下墓碑：身份不可改文复用，否则同一
            # AssetRef 会在不同时间指向不同正文，破坏不可变语义。
            tombstone_row = self._conn.execute(
                "SELECT detail_json FROM asset_events"
                " WHERE asset_id=? AND version=? AND action='collect'"
                " ORDER BY event_id DESC LIMIT 1",
                (asset_id, version)).fetchone()
            if tombstone_row is not None:
                detail = self._load(tombstone_row["detail_json"])
                tombstone = (detail.get("content_digest")
                             if isinstance(detail, dict) else None)
                raise AssetConflict(
                    f"{asset_id}@{version} 已被回收，版本身份不可改文复用"
                    f"（原摘要 {tombstone or 'unknown'}）—— 请登记新版本")

            self._conn.execute(
                "INSERT INTO assets (asset_id, version, asset_type, namespace,"
                " lifecycle_state, content_digest, content_json,"
                " source_refs_json, dependency_refs_json, compatibility_json,"
                " evaluation_evidence_refs_json, rollback_ref,"
                " created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (asset.asset_id, asset.version, asset.asset_type,
                 asset.namespace, asset.lifecycle_state, asset.content_digest,
                 json.dumps(asset.content, ensure_ascii=False, sort_keys=True),
                 self._dump(asset.source_refs),
                 self._dump(asset.dependency_refs),
                 self._dump(asset.compatibility),
                 self._dump(asset.evaluation_evidence_refs),
                 asset.rollback_ref, asset.created_at, asset.updated_at))
            self._event(asset.ref, "register", None, asset.lifecycle_state,
                        {"content_digest": digest,
                         "asset_type": asset.asset_type,
                         "namespace": asset.namespace})
            self._conn.commit()
            return asset

    def register_draft(self, draft: AssetDraft) -> Asset:
        return self.register(
            asset_id=draft.asset_id, asset_type=draft.asset_type,
            namespace=draft.namespace, version=draft.version,
            content=draft.content, lifecycle_state=draft.lifecycle_state,
            source_refs=draft.source_refs,
            dependency_refs=draft.dependency_refs,
            compatibility=draft.compatibility,
            evaluation_evidence_refs=draft.evaluation_evidence_refs,
            rollback_ref=draft.rollback_ref)

    # ---- 精确读取（没有“最新版本”入口）----
    def get(self, asset_id: str, version: str) -> Optional[Asset]:
        row = self._conn.execute(
            "SELECT * FROM assets WHERE asset_id=? AND version=?",
            (asset_id, version)).fetchone()
        return self._row_to_asset(row) if row else None

    def get_exact(self, ref: str) -> Asset:
        parsed = AssetRef.parse(ref)
        asset = self.get(parsed.asset_id, parsed.version)
        if asset is None:
            raise AssetNotFound(f"资产不存在: {parsed}")
        return asset

    def resolve(self, ref: str) -> Dict[str, Any]:
        return self.get_exact(ref).to_contract_dict()

    def versions_of(self, asset_id: str, *,
                    lifecycle_state: Optional[str] = None) -> List[Asset]:
        """同一 asset_id 的全部版本（只读；供消费方做确定性选择）。"""
        sql = "SELECT * FROM assets WHERE asset_id=?"
        args: List[Any] = [asset_id]
        if lifecycle_state:
            sql += " AND lifecycle_state=?"
            args.append(lifecycle_state)
        sql += " ORDER BY version"
        return [self._row_to_asset(r) for r in self._conn.execute(sql, args)]

    def list(self, *, asset_type: Optional[str] = None,
             namespace: Optional[str] = None,
             lifecycle_state: Optional[str] = None,
             limit: int = 500) -> List[Asset]:
        sql = "SELECT * FROM assets WHERE 1=1"
        args: List[Any] = []
        if asset_type:
            sql += " AND asset_type=?"
            args.append(asset_type)
        if namespace:
            sql += " AND namespace=?"
            args.append(namespace)
        if lifecycle_state:
            sql += " AND lifecycle_state=?"
            args.append(lifecycle_state)
        sql += " ORDER BY asset_type, asset_id, version LIMIT ?"
        args.append(int(limit))
        return [self._row_to_asset(r) for r in self._conn.execute(sql, args)]

    # ---- 生命周期 ----
    def transition(self, asset_id: str, version: str, to_state: str,
                   *, evidence_refs: Sequence[str] = ()
                   ) -> Asset:
        with self._locked():
            asset = self.get(asset_id, version)
            if asset is None:
                raise AssetNotFound(f"资产不存在: {asset_id}@{version}")
            ensure_transition(asset.lifecycle_state, to_state)

            merged = list(asset.evaluation_evidence_refs)
            for item in evidence_refs:
                if item and item not in merged:
                    merged.append(item)
            if to_state in ("approved", "active"):
                if not merged:
                    raise LifecycleError(
                        f"{asset.ref} 晋升 {to_state} 需要评测证据引用")
                if not asset.source_refs:
                    raise LifecycleError(
                        f"{asset.ref} 晋升 {to_state} 需要来源引用")
                self._check_dependencies_audited(asset)

            now = time.time()
            self._conn.execute(
                "UPDATE assets SET lifecycle_state=?,"
                " evaluation_evidence_refs_json=?, updated_at=?"
                " WHERE asset_id=? AND version=?",
                (to_state, self._dump(tuple(merged)), now, asset_id, version))
            self._event(asset.ref, "transition", asset.lifecycle_state,
                        to_state, {"evidence_refs": merged})
            self._conn.commit()
            return self.get(asset_id, version)

    # ---- 依赖解析与兼容检查 ----
    def check_dependencies(self, asset: Asset) -> Dict[str, Asset]:
        """返回依赖表；缺失/环/兼容不满足时抛 DependencyError。"""
        with self._locked():
            return self._check_dependencies_audited(asset)

    def _check_dependencies_audited(self, asset: Asset) -> Dict[str, Asset]:
        try:
            deps = self._resolve_dependencies(asset)
        except DependencyError as exc:
            self._event(asset.ref, "dependency_check", None, None,
                        {"ok": False, "error": str(exc)})
            self._conn.commit()
            raise
        self._event(asset.ref, "dependency_check", None, None,
                    {"ok": True, "dependencies": sorted(deps),
                     "pins": sorted((asset.compatibility.get("pins") or {}))})
        self._conn.commit()
        return deps

    def _resolve_dependencies(self, asset: Asset) -> Dict[str, Asset]:
        deps: Dict[str, Asset] = {}
        missing: List[str] = []
        for ref_text in asset.dependency_refs:
            ref = AssetRef.parse(ref_text)
            dep = self.get(ref.asset_id, ref.version)
            if dep is None:
                missing.append(str(ref))
            else:
                deps[str(ref)] = dep
        if missing:
            raise DependencyError(
                f"{asset.ref} 缺少依赖: {', '.join(sorted(missing))}")
        self._assert_acyclic(asset)
        return deps

    def _assert_acyclic(self, root: Asset) -> None:
        visiting: set = set()
        done: set = set()

        def visit(asset: Asset) -> None:
            key = str(asset.ref)
            if key in visiting:
                raise DependencyError(
                    f"依赖环: {key} 再次进入当前解析栈")
            if key in done:
                return
            visiting.add(key)
            dependencies = {}
            for ref_text in asset.dependency_refs:
                ref = AssetRef.parse(ref_text)
                dep = self.get(ref.asset_id, ref.version)
                if dep is None:
                    raise DependencyError(
                        f"{asset.ref} 缺少依赖: {ref}")
                dependencies.setdefault(dep.asset_id, []).append(dep)
                visit(dep)
            for dep_id, expected in (asset.compatibility.get("pins") or {}).items():
                versions = dependencies.get(dep_id)
                if not versions:
                    raise DependencyError(f"{asset.ref} 的兼容性 pin 指向未声明的依赖 {dep_id}")
                if any(dep.content_digest != expected for dep in versions):
                    raise DependencyError(f"{asset.ref} 与 {dep_id} 不兼容：依赖摘要不符合 pin {expected}")
            visiting.discard(key)
            done.add(key)

        visit(root)

    def dependents(self, asset_id: str, version: str) -> List[AssetRef]:
        """谁精确引用了这个版本（别名不会产生引用）。"""
        target = str(AssetRef(asset_id, version))
        out: List[AssetRef] = []
        rows = self._conn.execute(
            "SELECT asset_id, version, dependency_refs_json, rollback_ref FROM assets")
        for row in rows:
            refs = self._load(row["dependency_refs_json"])
            if target in refs or target == row["rollback_ref"]:
                out.append(AssetRef(row["asset_id"], row["version"]))
        return sorted(out, key=str)

    def rollback_asset(self, asset_id: str, version: str) -> Asset:
        asset = self.get(asset_id, version)
        if asset is None:
            raise AssetNotFound(f"资产不存在: {asset_id}@{version}")
        if not asset.rollback_ref:
            raise LifecycleError(f"{asset.ref} 没有回滚引用")
        return self.get_exact(asset.rollback_ref)

    # ---- 退役与 GC ----
    def gc_candidates(self) -> List[Dict[str, Any]]:
        """只返回 retired 且无人引用的版本；在用版本绝不进入候选。"""
        out = []
        rows = self._conn.execute(
            "SELECT * FROM assets WHERE lifecycle_state='retired'")
        for row in rows:
            asset = self._row_to_asset(row)
            if (not self.dependents(asset.asset_id, asset.version) and
                    not persisted_references(self._conn, asset.ref)):
                out.append(asset.summary())
        return out

    def collect(self, asset_id: str, version: str) -> bool:
        with self._locked():
            asset = self.get(asset_id, version)
            if asset is None:
                raise AssetNotFound(f"资产不存在: {asset_id}@{version}")
            if asset.lifecycle_state != "retired":
                raise LifecycleError(f"{asset.ref} 未退役，不能回收")
            refs = self.dependents(asset_id, version)
            if refs:
                raise DependencyError(
                    f"{asset.ref} 仍被引用: {', '.join(map(str, refs))}")
            pinned = persisted_references(self._conn, asset.ref)
            if pinned:
                raise DependencyError(f"{asset.ref} 仍被冻结发布或运行引用: {', '.join(pinned)}")
            self._conn.execute(
                "DELETE FROM assets WHERE asset_id=? AND version=?",
                (asset_id, version))
            self._event(asset.ref, "collect", "retired", None,
                        {"content_digest": asset.content_digest})
            self._conn.commit()
            return True

    # ---- 追溯 ----
    def events(self, asset_id: Optional[str] = None,
               version: Optional[str] = None,
               limit: int = 100) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM asset_events WHERE 1=1"
        args: List[Any] = []
        if asset_id:
            sql += " AND asset_id=?"
            args.append(asset_id)
        if version:
            sql += " AND version=?"
            args.append(version)
        sql += " ORDER BY event_id ASC LIMIT ?"
        args.append(int(limit))
        return [{
            "event_id": r["event_id"],
            "asset_id": r["asset_id"],
            "version": r["version"],
            "action": r["action"],
            "from_state": r["from_state"],
            "to_state": r["to_state"],
            "detail": self._load(r["detail_json"]),
            "created_at": r["created_at"],
        } for r in self._conn.execute(sql, args)]

    # ---- 内部 ----
    def _event(self, ref: AssetRef, action: str, from_state: Optional[str],
               to_state: Optional[str], detail: Dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO asset_events (asset_id, version, action,"
            " from_state, to_state, detail_json, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (ref.asset_id, ref.version, action, from_state, to_state,
             json.dumps(detail, ensure_ascii=False, sort_keys=True),
             time.time()))

    def _row_to_asset(self, row: sqlite3.Row) -> Asset:
        try:
            content = json.loads(row["content_json"])
        except (TypeError, ValueError) as exc:
            raise AssetValidationError(
                f"资产库内容损坏: {row['asset_id']}@{row['version']}") from exc
        if not isinstance(content, dict) or content_digest(content) != row["content_digest"]:
            raise AssetValidationError(f"资产库正文摘要不符: {row['asset_id']}@{row['version']}")
        asset = Asset(
            asset_id=row["asset_id"], version=row["version"],
            asset_type=row["asset_type"], namespace=row["namespace"],
            content_digest=row["content_digest"],
            source_refs=tuple(self._load(row["source_refs_json"])),
            dependency_refs=tuple(self._load(row["dependency_refs_json"])),
            compatibility=dict(self._load(row["compatibility_json"])),
            lifecycle_state=row["lifecycle_state"],
            evaluation_evidence_refs=tuple(
                self._load(row["evaluation_evidence_refs_json"])),
            rollback_ref=row["rollback_ref"], content=content,
            created_at=row["created_at"], updated_at=row["updated_at"])
        asset.validate()
        return asset

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _load(text: Any) -> Any:
        if text in (None, ""):
            return []
        return json.loads(text)


def _as_sequence(value: Any) -> Tuple[str, ...]:
    """把请求里的字符串数组规范化；单个字符串是常见误用，直接拒绝。"""
    if value is None:
        return ()
    if isinstance(value, str):
        raise AssetValidationError("该字段必须是字符串数组，不能是单个字符串")
    if not isinstance(value, (list, tuple)):
        raise AssetValidationError("该字段必须是字符串数组")
    for item in value:
        if not isinstance(item, str) or not item:
            raise AssetValidationError("数组元素必须是非空字符串")
    return tuple(value)


def repository_for(db: Any) -> AssetRepository:
    """从共享 Database 取资产仓储（不改 persistence 的公共方法面）。"""
    conn = getattr(db, "_conn", None)
    if conn is None:
        raise AssetValidationError("Database 缺少连接，无法建立资产仓储")
    # 共享库已由 core/schema 域建表；复用其写锁，避免并发登记/迁移交错。
    return AssetRepository(conn, ensure_schema=False,
                           lock=getattr(db, "_lock", None))
