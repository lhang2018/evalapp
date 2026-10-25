"""ALN-17-P：空机部署、组合恢复与 RTO/RPO 测试。

覆盖验收：固定备份/版本可复现；不兼容拒绝（不动数据）；失败回退与真实
时间/损失窗口记录。备份/恢复复用 `core/backup.py` 已接受切片。
"""

import json
import sqlite3
import zipfile

import pytest

from evoagent.app import App
from evoagent.core.backup import MANIFEST_NAME
from evoagent.core.schema.migrations import SCHEMA_VERSION
from evoagent.core.task import Task
from evoagent.deployment.bootstrap import (DeploymentError, MANIFEST_NAME as DEPLOY,
                                           bootstrap, load, verify)
from evoagent.recovery.combined import (CompositeRecovery, IncompatibleBackup,
                                        RecoveryPolicy, forge_incompatible_backup)


@pytest.fixture
def host(tmp_path):
    data_dir = tmp_path / "host"
    manifest = bootstrap(str(data_dir), app_version="test-1", source_commit="abc123")
    return data_dir, manifest


def opened(data_dir):
    app = App(data_dir=str(data_dir))
    return app


def seed(app, *, goal="seed task"):
    app.db.save_task(Task(user_goal=goal))
    app.bus.emit(event_type="seed_probe", component_id="test",
                 payload={"goal": goal})


def test_clean_host_bootstrap_and_verify(host):
    data_dir, manifest = host
    assert manifest["instance_id"].startswith("inst_")
    assert manifest["schema_version_source"] == SCHEMA_VERSION
    for name in ("evoagent.db", "events.db", "memory.db", "deployment.json"):
        assert (data_dir / name).exists()
    result = verify(str(data_dir))
    assert result["ok"] is True and result["errors"] == []
    assert result["instance_id"] == manifest["instance_id"]
    assert load(str(data_dir))["app_version"] == "test-1"
    with pytest.raises(DeploymentError):
        bootstrap(str(data_dir), app_version="again")


def test_bootstrap_refuses_residual_files(tmp_path):
    target = tmp_path / "dirty"
    target.mkdir()
    (target / "leftover.txt").write_text("not empty", encoding="utf-8")
    with pytest.raises(DeploymentError):
        bootstrap(str(target))


def test_verify_detects_missing_database_and_schema_drift(host):
    data_dir, _ = host
    (data_dir / "events.db").unlink()
    missing = verify(str(data_dir))
    assert missing["ok"] is False
    assert any("缺少数据库文件" in item for item in missing["errors"])
    # 重建后再制造 schema 漂移
    bootstrap(str(data_dir), app_version="rebuild", allow_existing=True)
    conn = sqlite3.connect(str(data_dir / "evoagent.db"))
    try:
        conn.execute("DELETE FROM schema_migrations WHERE version=?", (SCHEMA_VERSION,))
        conn.commit()
    finally:
        conn.close()
    drifted = verify(str(data_dir))
    assert drifted["ok"] is False
    assert any("schema 版本" in item for item in drifted["errors"])


def test_composite_restore_measures_rto_rpo_and_keeps_identity(host):
    data_dir, manifest = host
    app = opened(data_dir)
    try:
        seed(app)
        backup = app.backups.create("seeded.zip")
        seed(app, goal="post backup change")   # 备份后增量，恢复应被丢弃
        before = app.backups._counts()
        recovery = CompositeRecovery(
            str(data_dir), db=app.db, bus=app.bus, memory=app.memory,
            releases=app.releases,
            policy=RecoveryPolicy(rto_target_s=300, rpo_target_s=600))
        report = recovery.restore(backup["name"], incident_at=None)
        assert report["ok"] is True
        assert report["identity_preserved"] is True
        assert report["counts_matched"] is True
        assert report["counts_after"]["tasks"] == backup["counts"]["tasks"]
        assert report["counts_after"]["tasks"] < before["tasks"]
        assert report["rto_s"] >= 0
        assert report["rpo_s"] >= 0
        assert report["data_loss_window"]["from_backup_at"] == pytest.approx(
            backup["created_at"])
        assert report["policy"]["meets_targets"] is True
        assert report["safety_backup"] and (data_dir / "backups" / report["safety_backup"]).exists()
    finally:
        app.db.close()


def test_incompatible_backup_is_refused_without_touching_data(host):
    data_dir, _ = host
    app = opened(data_dir)
    try:
        seed(app)
        backup = app.backups.create("seeded.zip")
        forged = forge_incompatible_backup(app.backups, backup["name"])
        before = app.backups._counts()
        recovery = CompositeRecovery(str(data_dir), db=app.db, bus=app.bus,
                                     memory=app.memory)
        with pytest.raises(IncompatibleBackup):
            recovery.restore(forged)
        assert app.backups._counts() == before
    finally:
        app.db.close()


def test_failed_restore_rolls_back_to_safety_backup(host):
    data_dir, _ = host
    app = opened(data_dir)
    try:
        seed(app)
        backup = app.backups.create("seeded.zip")
        seed(app, goal="keep me")              # 失败后必须保留当前状态
        before = app.backups._counts()
        recovery = CompositeRecovery(str(data_dir), db=app.db, bus=app.bus,
                                     memory=app.memory)

        def boom(_manager):
            raise RuntimeError("受控故障注入：恢复中途失败")

        report = recovery.restore_with_rollback(backup["name"], inject_failure=boom)
        assert report["ok"] is False
        assert "受控故障注入" in report["error"]
        assert report["rollback"]["ok"] is True
        assert report["rollback"]["counts_match_pre_failure"] is True
        assert report["rollback"]["rto_s"] >= 0
        assert app.backups._counts() == before
        assert report["identity_preserved"] is True
    finally:
        app.db.close()


def test_matrix_records_positive_refusal_and_rollback_scenarios(host):
    data_dir, _ = host
    app = opened(data_dir)
    try:
        seed(app)
        backup = app.backups.create("seeded.zip")
        forged = forge_incompatible_backup(app.backups, backup["name"])
        recovery = CompositeRecovery(str(data_dir), db=app.db, bus=app.bus,
                                     memory=app.memory)
        scenarios = recovery.matrix(backup_ref=backup["name"],
                                    incompatible_ref=forged)
        by_name = {item["scenario"]: item for item in scenarios}
        assert by_name["compatible_restore"]["result"]["ok"] is True
        assert by_name["incompatible_refusal"]["result"]["rejected"] is True
        assert by_name["incompatible_refusal"]["elapsed_s"] >= 0
        rollback = by_name["failure_rollback"]["result"]
        assert rollback["ok"] is False and rollback["rollback"]["ok"] is True
        assert rollback["rollback"]["counts_match_pre_failure"] is True
    finally:
        app.db.close()


def test_forged_backup_keeps_original_data_and_manifest(host):
    data_dir, _ = host
    app = opened(data_dir)
    try:
        seed(app)
        backup = app.backups.create("seeded.zip")
        forged = forge_incompatible_backup(app.backups, backup["name"])
        with zipfile.ZipFile(data_dir / "backups" / forged) as archive:
            manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
        assert manifest["schema_version"] == SCHEMA_VERSION + 5
        original = app.backups.verify(backup["name"])
        assert original["ok"] is True
        assert original["manifest"]["schema_version"] == SCHEMA_VERSION
    finally:
        app.db.close()
