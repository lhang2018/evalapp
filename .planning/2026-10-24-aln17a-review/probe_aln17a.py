"""ALN-17-A 独立复核探针（claude-main 自写，不引用作者测试）。

覆盖卡里列明的四个待复核点：跨版本判定、暂存迁移与行数核对、
SAVEPOINT 回退语义、RTO/RPO 口径；另加两根护栏防「全拒绝」假绿。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

RESULTS: list = []


def probe(name, fn):
    try:
        fn()
        RESULTS.append((name, True, ""))
        print(f"[PASS] {name}")
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")


def _app(tag="17a"):
    from evoagent.app import App
    return App(data_dir=tempfile.mkdtemp(prefix=f"aln17a-{tag}-"))


def _rewrite(src: Path, name: str, mutate) -> str:
    """只改 manifest.json，其余文件字节原样保留（不伪造数据文件）。"""
    out = src.parent / name
    with zipfile.ZipFile(src) as zf:
        items = {n: zf.read(n) for n in zf.namelist()}
    manifest = json.loads(items["manifest.json"].decode("utf-8"))
    mutate(manifest)
    items["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for n, blob in items.items():
            dst.writestr(n, blob)
    return name


def _path_of(app, name):
    return app.backups.backup_dir / name


# ---------- R1 跨版本拒绝（备份比程序新） ----------

def r1_newer_backup_rejected_with_actionable_reason():
    from evoagent.core.schema.migrations import SCHEMA_VERSION
    app = _app("r1")
    try:
        base = app.backups.create("base.zip")["name"]
        future = _rewrite(_path_of(app, base), "future.zip",
                          lambda m: m.__setitem__("schema_version",
                                                  SCHEMA_VERSION + 7))
        try:
            app.backups.restore(future)
        except ValueError as exc:
            msg = str(exc)
            assert "升级" in msg or "upgrade" in msg.lower(), (
                f"拒绝理由不可操作（未提示升级程序）: {msg}")
            return
        raise AssertionError("更高版本备份被接受恢复（程序会读到未知结构）")
    finally:
        app.db.close()


# ---------- R2 旧格式（缺 schema_version） ----------

def r2_backup_without_schema_version_rejected():
    app = _app("r2")
    try:
        base = app.backups.create("base.zip")["name"]
        legacy = _rewrite(_path_of(app, base), "legacy.zip",
                          lambda m: m.pop("schema_version", None))
        check = app.backups.verify(legacy)
        assert not check["ok"], (
            "缺 schema_version 的旧格式备份被判为可恢复（无法判断兼容性）")
        assert check.get("errors"), "拒绝但没给出原因"
    finally:
        app.db.close()


# ---------- R3 行数快照核对 ----------

def r3_count_mismatch_aborts_without_touching_current_data():
    app = _app("r3")
    try:
        base = app.backups.create("base.zip")["name"]
        before = dict(app.backups._counts())

        def bump(manifest):
            counts = manifest.get("counts") or {}
            key = next(iter(counts))
            counts[key] = int(counts[key]) + 999

        tampered = _rewrite(_path_of(app, base), "counts.zip", bump)
        try:
            app.backups.restore(tampered)
        except ValueError as exc:
            assert "行数" in str(exc) or "快照" in str(exc), (
                f"拒绝理由未说明行数不符: {exc}")
        else:
            raise AssertionError("行数快照不符却完成了恢复（可能已覆盖现网数据）")
        assert dict(app.backups._counts()) == before, (
            "恢复被中止，但当前数据已被改动")
    finally:
        app.db.close()


# ---------- R4 SAVEPOINT 回退语义 ----------

def r4_failed_migration_rolls_back_only_itself():
    from evoagent.core.schema.migrations import (
        MIGRATIONS, SCHEMA_VERSION, MigrationError, apply_migrations,
        current_version)
    tmp = Path(tempfile.mkdtemp(prefix="aln17a-mig-")) / "m.db"
    conn = sqlite3.connect(str(tmp))
    try:
        apply_migrations(conn)
        applied_before = current_version(conn)

        def bad(inner):
            inner.execute("CREATE TABLE leaked_partial (x INTEGER)")
            inner.execute("INSERT INTO leaked_partial VALUES (1)")
            raise RuntimeError("probe-boom")

        MIGRATIONS.append((SCHEMA_VERSION + 1, "probe_fail", bad))
        try:
            try:
                apply_migrations(conn)
            except MigrationError as exc:
                assert exc.version == SCHEMA_VERSION + 1, \
                    f"MigrationError 版本号不对: {exc.version}"
                assert exc.name == "probe_fail"
                assert "probe-boom" in str(exc.cause)
            else:
                raise AssertionError("失败迁移没有抛出 MigrationError")
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert "leaked_partial" not in tables, (
                "失败迁移的部分变更残留（SAVEPOINT 回退未覆盖建表）")
            assert current_version(conn) == applied_before, (
                "失败迁移影响了迁移台账版本")
            rows = conn.execute(
                "SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            assert rows == applied_before, (
                f"失败迁移被记入台账: {rows} vs {applied_before}")
        finally:
            MIGRATIONS.pop()
    finally:
        conn.close()


# ---------- R5 护栏：正常往返 ----------

def r5_normal_roundtrip_still_works():
    app = _app("r5")
    try:
        base = app.backups.create("base.zip")["name"]
        before = dict(app.backups._counts())
        result = app.backups.restore(base)
        assert result.get("counts_verified") is True, (
            f"恢复完成但未标记计数已核对: {result}")
        assert dict(app.backups._counts()) == before, "恢复后计数与快照不一致"
        assert result.get("schema_version") is not None
        assert isinstance(result.get("migrations_applied"), list)
    finally:
        app.db.close()


# ---------- R6 RTO/RPO 口径 ----------

def r6_duration_and_counts_are_measurable():
    app = _app("r6")
    try:
        base = app.backups.create("base.zip")["name"]
        result = app.backups.restore(base)
        duration = result.get("duration_ms")
        assert isinstance(duration, (int, float)) and duration >= 0, (
            f"未提供可用的恢复耗时口径: {duration!r}")
        assert duration < 600_000, f"耗时口径疑似单位错误: {duration}"
        # RPO：恢复后逐键计数须与备份快照完全一致（=0 丢失）
        manifest = app.backups.verify(base)["manifest"]
        assert manifest.get("counts"), "manifest 未记录行数快照，RPO 无法核算"
        for key, value in manifest["counts"].items():
            assert app.backups._counts().get(key) == value, (
                f"{key} 恢复后 {app.backups._counts().get(key)} ≠ 快照 {value}")
    finally:
        app.db.close()


if __name__ == "__main__":
    probe("R1 跨版本(备份更新)拒绝且理由可操作",
          r1_newer_backup_rejected_with_actionable_reason)
    probe("R2 缺 schema_version 旧格式拒绝",
          r2_backup_without_schema_version_rejected)
    probe("R3 行数快照不符中止且不动现网数据",
          r3_count_mismatch_aborts_without_touching_current_data)
    probe("R4 失败迁移只回退自身(SAVEPOINT)",
          r4_failed_migration_rolls_back_only_itself)
    probe("R5 正常备份恢复往返 (护栏)", r5_normal_roundtrip_still_works)
    probe("R6 耗时与计数口径可核算", r6_duration_and_counts_are_measurable)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\ntotal={len(RESULTS)} passed={passed} failed={len(RESULTS) - passed}")
    sys.exit(0 if passed == len(RESULTS) else 1)
