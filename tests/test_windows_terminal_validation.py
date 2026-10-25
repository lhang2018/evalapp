"""ALN-21-A：Windows 终端扩展验证（本机真实执行，非夹具冒充真机）。

覆盖任务卡 21-A 验收：固定端版本/身份/回执；终端差异明确；不复制第二套
执行权威；状态恢复。复用已验收切片（不重新实现）：
- 17-P：空机部署（deployment.bootstrap）与组合恢复（recovery.combined）；
- 11-P：生产签名身份（release.identity）与回执信任绑定（release.delivery）；
- 20-P：能力委派既有权威、不复制第二套状态机的模式；
- 07-C：接管恢复先对账后决定的闸。

本卡为验证卡（沿用 12-V 真机验证卡模式）：除平台词汇表的最小加法扩展
（manager/delivery 各一行，使 Windows 终端可走回执对账）外不改产品源码，
交付物为可复跑的验证测试 + 实测证据 + 终端差异矩阵。

真实进程击杀场景以子进程真实死亡验证（文件同步 + 宽限轮询，不用休眠竞态）。
"""

import hashlib
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import pytest

from evoagent.app import App
from evoagent.core.task import Task, TaskStatus
from evoagent.deployment.bootstrap import bootstrap, load, verify
from evoagent.recovery.combined import (CompositeRecovery, IncompatibleBackup,
                                        forge_incompatible_backup)
from evoagent.release.delivery import ReleaseDelivery
from evoagent.release.identity import IdentityRegistry, PrivateIdentityKey
from evoagent.release.manager import ReleaseManager
from evoagent.tools.contract import ToolContract, ToolResult
from evoagent.workflows.models import WorkflowDef, WorkflowNode

REPO_ROOT = Path(__file__).resolve().parents[1]

# 真实进程击杀探针：工具就绪即落盘 task_id 并驻留，父进程随后真实击杀。
_RESTART_WORKER = '''
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[3])
from evoagent.app import App
from evoagent.core.task import Task
from evoagent.tools.contract import ToolContract, ToolResult
from evoagent.workflows.models import WorkflowDef, WorkflowNode

data_dir, ready_file = sys.argv[1], sys.argv[2]
app = App(data_dir=data_dir)
app.controller.runs.leases.ttl = 0.5

def probe(_):
    task_id = app.db.list_tasks()[0].task_id
    Path(ready_file).write_text(task_id, encoding="utf-8")
    time.sleep(90)
    return ToolResult(ok=True, submitted=True, executed=True,
                      contract_valid=True, goal_reached=True,
                      independently_verified=True)

app.permissions.high_risk_mode = "allow"
app.tools.register(ToolContract(tool_id="restart_probe",
                                side_effect_level="none",
                                timeout_policy=120), probe)
wf = WorkflowDef(workflow_id="restart-probe",
                 nodes=[WorkflowNode(node_id="go", tool="restart_probe")])
app._workflows[wf.workflow_id] = wf
app.controller.execute(
    Task(user_goal="Windows 终端重启恢复探针", workflow_ref="restart-probe"),
    intent_hint="general")
'''


class _MemoryStore:
    """最小安装/部署存储（与 test_release_install_retire 同型）。"""

    def __init__(self):
        self.installations = {}
        self.deployments = {}

    def get_installation(self, installation_id):
        return self.installations.get(installation_id)

    def save_installation(self, installation_id, device_id, platform,
                          app_version, current_release_id, target_release_id,
                          status, data):
        self.installations[installation_id] = {
            "installation_id": installation_id,
            "device_id": device_id,
            "platform": platform,
            "app_version": app_version,
            "current_release_id": current_release_id,
            "target_release_id": target_release_id,
            "status": status,
            "data": data,
        }

    def get_device_deployment(self, deployment_id):
        return self.deployments.get(deployment_id)

    def save_device_deployment(self, deployment_id, installation_id,
                               release_id, action, outcome, data):
        self.deployments[deployment_id] = {
            "deployment_id": deployment_id,
            "installation_id": installation_id,
            "release_id": release_id,
            "action": action,
            "outcome": outcome,
            "data": data,
        }


def _signed_release():
    """生产签名身份签发的 Windows 目标制品（复用 11-P 签发链）。"""
    rm = ReleaseManager()
    key = PrivateIdentityKey.generate(key_id="win-prod-key-1")
    body = b'{"workflow": "terminal/v1"}'
    digest = hashlib.sha256(body).hexdigest()
    manifest = rm.build_manifest(
        release_id="rel_win_1", version="v1.0.0", channel="stable",
        target_platforms=["windows"], min_app_version="1.0.0",
        bundle_kind="workflow",
        artifact_url="https://example.invalid/rel_win_1",
        artifact_sha256=digest, artifact_size=len(body),
        artifact_signature=key.sign_artifact_envelope(
            release_id="rel_win_1", version="v1.0.0",
            artifact_sha256=digest, artifact_size=len(body)),
        artifact_signing_key_id=key.key_id,
        signature_schema="evoagent.release.signing/1")
    rm.current_release_id = manifest.release_id
    return rm, key, digest


@pytest.fixture
def terminal(tmp_path):
    """Windows 终端宿主：空机部署 + 固定版本基线（17-P bootstrap）。"""
    data_dir = tmp_path / "win-terminal"
    manifest = bootstrap(str(data_dir), app_version="win-1.0.0",
                         source_commit="aln21a-probe")
    return data_dir, manifest


def _recovery(data_dir, app):
    return CompositeRecovery(str(data_dir), db=app.db, bus=app.bus,
                             memory=app.memory)


class TestPinnedTerminalVersion:
    """固定端版本：部署清单钉住源码/结构版本，漂移即 fail-closed。"""

    def test_bootstrap_pins_app_and_schema_versions(self, terminal):
        data_dir, manifest = terminal
        assert manifest["instance_id"].startswith("inst_")
        assert manifest["app_version"] == "win-1.0.0"
        result = verify(str(data_dir))
        assert result["ok"] is True and result["errors"] == []
        assert load(str(data_dir))["app_version"] == "win-1.0.0"

    def test_verify_fails_closed_on_missing_database(self, terminal):
        data_dir, _ = terminal
        (data_dir / "events.db").unlink()
        result = verify(str(data_dir))
        assert result["ok"] is False
        assert result["errors"]

    def test_verify_fails_closed_on_schema_drift(self, terminal):
        data_dir, _ = terminal
        import sqlite3
        from evoagent.core.schema.migrations import SCHEMA_VERSION
        conn = sqlite3.connect(str(data_dir / "evoagent.db"))
        try:
            conn.execute("DELETE FROM schema_migrations WHERE version=?",
                         (SCHEMA_VERSION,))
            conn.commit()
        finally:
            conn.close()
        result = verify(str(data_dir))
        assert result["ok"] is False
        assert any("schema" in item for item in result["errors"])

    def test_incompatible_backup_refused_before_touching_data(self, terminal):
        data_dir, _ = terminal
        app = App(data_dir=str(data_dir))
        try:
            app.db.save_task(Task(user_goal="版本门探针"))
            backup = app.backups.create("seeded.zip")
            forged = forge_incompatible_backup(app.backups, backup["name"])
            before = app.backups._counts()
            with pytest.raises(IncompatibleBackup):
                _recovery(data_dir, app).restore(forged)
            assert app.backups._counts() == before
        finally:
            app.db.close()


class TestTerminalIdentity:
    """身份：实例身份跨恢复保持；签名登记簿与三库备份的边界显式化。"""

    def test_recovery_preserves_instance_identity(self, terminal):
        data_dir, manifest = terminal
        app = App(data_dir=str(data_dir))
        try:
            app.db.save_task(Task(user_goal="身份探针"))
            backup = app.backups.create("pre-change.zip")
            app.db.save_task(Task(user_goal="备份后变更"))
            report = _recovery(data_dir, app).restore(backup["name"])
            assert report["ok"] is True
            assert report["identity_preserved"] is True
            assert report["counts_matched"] is True
            assert verify(str(data_dir))["instance_id"] == \
                manifest["instance_id"]
        finally:
            app.db.close()

    def test_identity_registry_is_outside_three_db_backup(self, terminal):
        """登记簿是活信任件（公钥/撤销态），不属于数据三库备份。

        恢复不得把它回滚到旧快照——那会让已撤销身份复活（fail-open）。
        验证：备份内容不含登记簿；恢复不触碰登记簿文件；备份后登记的
        轮换在恢复后依然有效。
        """
        data_dir, _ = terminal
        app = App(data_dir=str(data_dir))
        try:
            registry_path = Path(str(data_dir)) / "identity.json"
            registry = IdentityRegistry(registry_path)
            registry.register_from_private(
                PrivateIdentityKey.generate(key_id="win-key-1"),
                actor="ops", reason="终端身份登记", activate=True)
            backup = app.backups.create("pre-rotate.zip")
            backup_file = data_dir / "backups" / backup["name"]
            with zipfile.ZipFile(backup_file) as zf:
                assert not any("identity" in name for name in zf.namelist())
            before = hashlib.sha256(registry_path.read_bytes()).hexdigest()
            registry.register_from_private(
                PrivateIdentityKey.generate(key_id="win-key-2"),
                actor="ops", reason="轮换")
            rotated = hashlib.sha256(registry_path.read_bytes()).hexdigest()
            assert before != rotated
            report = _recovery(data_dir, app).restore(backup["name"])
            assert report["ok"] is True
            after = hashlib.sha256(registry_path.read_bytes()).hexdigest()
            assert after == rotated, "恢复不得回滚身份登记簿"
        finally:
            app.db.close()

    def test_registry_loss_degrades_explicitly_not_silently(self):
        """登记簿丢失/缺失时显式降级，不静默信任（11-P 边界设计）。"""
        rm, key, _ = _signed_release()
        store = _MemoryStore()
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "identity.json"
            registry = IdentityRegistry(registry_path)
            registry.register_from_private(key, actor="ops",
                                           reason="登记", activate=True)
            delivery = ReleaseDelivery(rm, store,
                                       identity_registry=registry)
            out = delivery.reconcile(installation_id="inst_win",
                                     device_id="dev_win", platform="windows",
                                     app_version="1.0.0")
            assert out["target"]["signing_identity_status"] == "usable"
            # 场景 A：登记簿文件丢失 → 空登记簿 → unregistered，激活被拒
            registry_path.unlink()
            lost = ReleaseDelivery(rm, store,
                                   identity_registry=IdentityRegistry(
                                       registry_path))
            out_a = lost.reconcile(installation_id="inst_win",
                                   device_id="dev_win", platform="windows",
                                   app_version="1.0.0")
            assert out_a["decision"] == "blocked"
            failed = {c["name"]: c["detail"] for c in out_a["checks"]
                      if not c["ok"]}
            assert "signing_identity_trusted" in failed
            assert "unregistered" in failed["signing_identity_trusted"]
            # 场景 B：无登记簿服务 → 显式 no_registry 并记录，不静默信任
            bare = ReleaseDelivery(rm, store)
            out_b = bare.reconcile(installation_id="inst_win",
                                   device_id="dev_win", platform="windows",
                                   app_version="1.0.0")
            assert out_b["target"]["signing_identity_status"] == "no_registry"


class TestTerminalReceipts:
    """回执：Windows 终端走完整对账—激活—退役拒绝链（11-P 信任绑定）。"""

    def test_windows_platform_accepted_by_reconcile(self):
        """平台词汇表扩展后 Windows 终端可对账（本卡最小加法修改）。"""
        rm, key, _ = _signed_release()
        registry = IdentityRegistry()
        registry.register_from_private(key, actor="ops", reason="登记",
                                       activate=True)
        delivery = ReleaseDelivery(rm, _MemoryStore(),
                                   identity_registry=registry)
        out = delivery.reconcile(installation_id="inst_win",
                                 device_id="dev_win", platform="windows",
                                 app_version="1.0.0")
        assert out["decision"] == "update_available"
        assert out["target"]["release_id"] == "rel_win_1"

    def test_receipt_binds_server_resolved_identity(self):
        rm, key, digest = _signed_release()
        store = _MemoryStore()
        with tempfile.TemporaryDirectory() as tmp:
            registry = IdentityRegistry(Path(tmp) / "identity.json")
            registry.register_from_private(key, actor="ops",
                                           reason="登记", activate=True)
            delivery = ReleaseDelivery(rm, store,
                                       identity_registry=registry)
            delivery.reconcile(installation_id="inst_win", device_id="dev_win",
                               platform="windows", app_version="1.0.0")
            delivery.record_apply(installation_id="inst_win",
                                  action="verified", release_id="rel_win_1")
            result = delivery.record_apply(installation_id="inst_win",
                                           action="activated",
                                           release_id="rel_win_1")
            assert result["ok"] is True
            record = result["deployment"]
            assert record["signing_key_id"] == "win-prod-key-1"
            assert record["signing_identity_status"] == "usable"
            assert record["artifact_digest"] == digest

    def test_retired_release_receipt_rejected(self):
        rm, key, _ = _signed_release()
        store = _MemoryStore()
        with tempfile.TemporaryDirectory() as tmp:
            registry = IdentityRegistry(Path(tmp) / "identity.json")
            registry.register_from_private(key, actor="ops",
                                           reason="登记", activate=True)
            delivery = ReleaseDelivery(rm, store,
                                       identity_registry=registry)
            delivery.reconcile(installation_id="inst_win", device_id="dev_win",
                               platform="windows", app_version="1.0.0")
            delivery.record_apply(installation_id="inst_win",
                                  action="verified", release_id="rel_win_1")
            assert rm.retire("rel_win_1", reason="版本下线",
                             actor="ops")["ok"] is True
            blocked = delivery.record_apply(installation_id="inst_win",
                                            action="activated",
                                            release_id="rel_win_1")
            assert blocked["ok"] is False
            assert "退役" in blocked["error"]

    def test_self_reported_key_mismatch_rejected(self):
        """服务端权威：端侧自报密钥与清单解析值不一致即拒绝。"""
        rm, key, _ = _signed_release()
        store = _MemoryStore()
        with tempfile.TemporaryDirectory() as tmp:
            registry = IdentityRegistry(Path(tmp) / "identity.json")
            registry.register_from_private(key, actor="ops",
                                           reason="登记", activate=True)
            delivery = ReleaseDelivery(rm, store,
                                       identity_registry=registry)
            delivery.reconcile(installation_id="inst_win", device_id="dev_win",
                               platform="windows", app_version="1.0.0")
            delivery.record_apply(installation_id="inst_win",
                                  action="verified", release_id="rel_win_1")
            blocked = delivery.record_apply(installation_id="inst_win",
                                            action="activated",
                                            release_id="rel_win_1",
                                            signing_key_id="attacker-key")
            assert blocked["ok"] is False

    def test_installation_state_derives_from_single_store(self):
        """单一权威：全新实例从同一 store 重建出完全一致的安装状态。"""
        rm, key, _ = _signed_release()
        store = _MemoryStore()
        with tempfile.TemporaryDirectory() as tmp:
            registry = IdentityRegistry(Path(tmp) / "identity.json")
            registry.register_from_private(key, actor="ops",
                                           reason="登记", activate=True)
            first = ReleaseDelivery(rm, store, identity_registry=registry)
            first.reconcile(installation_id="inst_win", device_id="dev_win",
                            platform="windows", app_version="1.0.0")
            first.record_apply(installation_id="inst_win",
                               action="verified", release_id="rel_win_1")
            first.record_apply(installation_id="inst_win",
                               action="activated", release_id="rel_win_1")
            second = ReleaseDelivery(rm, store, identity_registry=registry)
            again = second.reconcile(installation_id="inst_win",
                                     device_id="dev_win", platform="windows",
                                     app_version="1.0.0")
            assert again["current_release_id"] == "rel_win_1"
            assert again["decision"] != "update_available"


class TestTerminalRestartRecovery:
    """状态恢复：真实进程死亡后由新终端实例接管续跑。"""

    def test_real_process_death_then_recovery_completes(self, terminal):
        data_dir, _ = terminal
        ready = data_dir / "ready.txt"
        proc = subprocess.Popen(
            [sys.executable, "-c", _RESTART_WORKER,
             str(data_dir), str(ready), str(REPO_ROOT)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            task_id = None
            deadline = time.time() + 60
            while time.time() < deadline:
                if ready.exists():
                    task_id = ready.read_text(encoding="utf-8").strip()
                    if task_id:
                        break
                if proc.poll() is not None:
                    _, err = proc.communicate()
                    pytest.fail(f"工作进程提前退出: {err[-600:]}")
                time.sleep(0.1)
            assert task_id, "探针工具未在 60s 内启动"
            proc.kill()          # 真实击杀，不优雅退出
            proc.wait(timeout=15)
            app2 = App(data_dir=str(data_dir))
            try:
                # 重启后的终端进程重新注册自己的工具契约与流程定义
                # （真实终端启动语义：契约随进程注册，不依赖已死进程的内存）
                app2.permissions.high_risk_mode = "allow"
                app2.tools.register(
                    ToolContract(tool_id="restart_probe",
                                 side_effect_level="none", timeout_policy=120),
                    lambda _: ToolResult(ok=True, submitted=True, executed=True,
                                         contract_valid=True, goal_reached=True,
                                         independently_verified=True))
                wf = WorkflowDef(
                    workflow_id="restart-probe",
                    nodes=[WorkflowNode(node_id="go", tool="restart_probe")])
                app2._workflows[wf.workflow_id] = wf
                task = app2.db.get_task(task_id)
                assert task is not None
                assert task.task_status == TaskStatus.RUNNING
                # 等待租约过期（持有者已真实死亡），显式接管
                deadline = time.time() + 20
                while time.time() < deadline:
                    try:
                        app2.controller.runs.leases.inspect_recovery(task)
                        break
                    except Exception:
                        time.sleep(0.2)
                else:
                    pytest.fail("租约未在 20s 内过期")
                recovered = app2.controller.recover_task(task_id, wait=True)
                assert recovered.is_terminal()
                assert recovered.task_status == TaskStatus.SUCCEEDED
                row = app2.db.get_task(task_id)
                assert row is not None and row.is_terminal()
            finally:
                app2.db.close()
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_composite_recovery_records_real_rto_and_loss_window(self, terminal):
        data_dir, _ = terminal
        app = App(data_dir=str(data_dir))
        try:
            app.db.save_task(Task(user_goal="RTO 探针"))
            backup = app.backups.create("rto.zip")
            time.sleep(0.05)
            app.db.save_task(Task(user_goal="备份后变更"))
            incident = time.time()
            report = _recovery(data_dir, app).restore(
                backup["name"], incident_at=incident)
            assert report["ok"] is True
            assert report["rto_s"] >= 0
            assert report["rpo_s"] >= 0
            assert report["data_loss_window"]["from_backup_at"] == \
                pytest.approx(backup["created_at"])
            assert report["data_loss_window"]["to_incident_at"] == \
                pytest.approx(incident)
        finally:
            app.db.close()


class TestNoSecondExecutionAuthority:
    """不复制第二套执行权威：单账本、单事件库、状态可自 store 重建。"""

    def test_recovery_leaves_single_task_ledger(self, terminal):
        data_dir, _ = terminal
        app = App(data_dir=str(data_dir))
        try:
            for i in range(3):
                app.db.save_task(Task(user_goal=f"账本探针 {i}"))
            backup = app.backups.create("ledger.zip")
            app.db.save_task(Task(user_goal="备份后多出的一条"))
            report = _recovery(data_dir, app).restore(backup["name"])
            assert report["ok"] is True
            assert report["counts_after"]["tasks"] == backup["counts"]["tasks"]
            ids = [r[0] for r in app.db._conn.execute(
                "SELECT task_id FROM tasks").fetchall()]
            assert len(ids) == len(set(ids)), "恢复后出现重复任务行"
        finally:
            app.db.close()

    def test_event_ledger_is_single_authority(self, terminal):
        data_dir, _ = terminal
        app = App(data_dir=str(data_dir))
        try:
            app.bus.emit(event_type="probe_a", component_id="test",
                         payload={"n": 1})
            backup = app.backups.create("events.zip")
            app.bus.emit(event_type="probe_b", component_id="test",
                         payload={"n": 2})
            report = _recovery(data_dir, app).restore(backup["name"])
            assert report["ok"] is True
            assert report["counts_after"]["events"] == \
                backup["counts"]["events"]
            assert (data_dir / "events.db").exists()
            assert not (data_dir / "events-2.db").exists()
        finally:
            app.db.close()
