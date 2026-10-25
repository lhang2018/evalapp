"""ALN-21-A：Windows 终端实测探针（本机真实执行，非夹具）。

在真实临时部署上跑完终端场景并落证据 JSON：
  环境绑定 → 空机部署（固定版本）→ 签发制品与回执链 → 真实进程击杀与
  接管续跑 → 组合恢复（真实 RTO/RPO）→ 单一权威核对。
判定与 tests/test_windows_terminal_validation.py 的断言一致；本脚本额外
绑定环境（源码 commit/Python/Windows 版本）并输出可归档的证据工件。

用法：python .planning/2026-10-25-aln21a/probe.py [--out <证据.json>]
"""

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evoagent.app import App  # noqa: E402
from evoagent.core.task import Task, TaskStatus  # noqa: E402
from evoagent.deployment.bootstrap import bootstrap, verify  # noqa: E402
from evoagent.recovery.combined import CompositeRecovery  # noqa: E402
from evoagent.release.delivery import ReleaseDelivery  # noqa: E402
from evoagent.release.identity import IdentityRegistry, PrivateIdentityKey  # noqa: E402
from evoagent.release.manager import ReleaseManager  # noqa: E402
from evoagent.tools.contract import ToolContract, ToolResult  # noqa: E402
from evoagent.workflows.models import WorkflowDef, WorkflowNode  # noqa: E402

_WORKER = '''
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
    Path(ready_file).write_text(app.db.list_tasks()[0].task_id, encoding="utf-8")
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
app.controller.execute(Task(user_goal="probe", workflow_ref="restart-probe"),
                       intent_hint="general")
'''


def _commit():
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                              capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except Exception:
        return "unknown"


def _signed_release():
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(
        ROOT / ".planning" / "2026-10-25-aln21a" /
        "windows-terminal-evidence.json"))
    args = parser.parse_args()

    evidence = {
        "card": "ALN-21-A",
        "environment": {
            "source_commit": _commit(),
            "python": platform.python_version(),
            "windows_release": list(platform.win32_ver()),
            "machine": platform.node(),
            "note": "Windows 即本卡目标终端（‘Windows 或平板’取 Windows）",
        },
        "scenarios": {},
    }

    tmp = tempfile.mkdtemp(prefix="aln21a-")
    try:
        data_dir = Path(tmp) / "win-terminal"

        # 场景 1：固定端版本——空机部署并核对版本基线
        try:
            manifest = bootstrap(str(data_dir), app_version="win-1.0.0",
                                 source_commit=evidence["environment"]
                                 ["source_commit"])
            check = verify(str(data_dir))
            evidence["scenarios"]["pinned_version"] = {
                "instance_id": manifest["instance_id"],
                "app_version": manifest["app_version"],
                "source_commit": manifest.get("source_commit"),
                "schema_version_source": manifest["schema_version_source"],
                "verify_ok": check["ok"],
                "verify_errors": check["errors"],
            }
        except Exception as exc:  # 探针不因单场景失败而丢失全部证据
            evidence["scenarios"]["pinned_version"] = {"error": repr(exc)}

        app = App(data_dir=str(data_dir))
        try:
            # 场景 2：身份与回执——Windows 平台对账 + 激活回执 + 退役拒绝
            try:
                rm, key, digest = _signed_release()
                registry = IdentityRegistry(Path(tmp) / "identity.json")
                registry.register_from_private(key, actor="ops",
                                               reason="终端身份登记",
                                               activate=True)
                delivery = ReleaseDelivery(rm, _Store(),
                                           identity_registry=registry)
                out = delivery.reconcile(installation_id="inst_win",
                                         device_id="dev_win",
                                         platform="windows",
                                         app_version="1.0.0")
                delivery.record_apply(installation_id="inst_win",
                                      action="verified",
                                      release_id="rel_win_1")
                activated = delivery.record_apply(installation_id="inst_win",
                                                  action="activated",
                                                  release_id="rel_win_1")
                record = activated.get("deployment") or {}
                rm.retire("rel_win_1", reason="探针退役", actor="ops")
                rejected = delivery.record_apply(installation_id="inst_win",
                                                 action="activated",
                                                 release_id="rel_win_1")
                evidence["scenarios"]["identity_and_receipts"] = {
                    "reconcile_decision": out["decision"],
                    "signing_identity_status":
                        out["target"]["signing_identity_status"],
                    "receipt_signing_key_id": record.get("signing_key_id"),
                    "receipt_identity_status":
                        record.get("signing_identity_status"),
                    "receipt_digest_matches":
                        record.get("artifact_digest") == digest,
                    "retired_receipt_rejected": not rejected["ok"],
                    "retired_reject_reason": rejected.get("error"),
                }
            except Exception as exc:
                evidence["scenarios"]["identity_and_receipts"] = {
                    "error": repr(exc)}

            # 场景 3：状态恢复——真实进程击杀后新终端实例接管续跑
            try:
                app.db.save_task(Task(user_goal="recovery anchor"))
                backup = app.backups.create("pre-kill.zip")
                ready = data_dir / "ready.txt"
                proc = subprocess.Popen(
                    [sys.executable, "-c", _WORKER,
                     str(data_dir), str(ready), str(ROOT)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True)
                task_id = None
                deadline = time.time() + 60
                while time.time() < deadline:
                    if ready.exists():
                        task_id = ready.read_text(
                            encoding="utf-8").strip()
                        if task_id:
                            break
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
                killed_ok = bool(task_id)
                if killed_ok:
                    proc.kill()
                    proc.wait(timeout=15)
                app2 = App(data_dir=str(data_dir))
                try:
                    app2.permissions.high_risk_mode = "allow"
                    app2.tools.register(
                        ToolContract(tool_id="restart_probe",
                                     side_effect_level="none",
                                     timeout_policy=120),
                        lambda _: ToolResult(ok=True, submitted=True,
                                             executed=True,
                                             contract_valid=True,
                                             goal_reached=True,
                                             independently_verified=True))
                    wf = WorkflowDef(
                        workflow_id="restart-probe",
                        nodes=[WorkflowNode(node_id="go",
                                            tool="restart_probe")])
                    app2._workflows[wf.workflow_id] = wf
                    pre = app2.db.get_task(task_id) if task_id else None
                    pre_status = pre.task_status.value if pre else None
                    deadline = time.time() + 20
                    while pre is not None and time.time() < deadline:
                        try:
                            app2.controller.runs.leases.inspect_recovery(pre)
                            break
                        except Exception:
                            time.sleep(0.2)
                    recovered = app2.controller.recover_task(task_id,
                                                             wait=True)
                    evidence["scenarios"]["restart_recovery"] = {
                        "worker_reached_tool": killed_ok,
                        "killed_exitcode": proc.returncode,
                        "pre_recover_status": pre_status,
                        "recovered_status": recovered.task_status.value,
                        "failure_reason": recovered.failure_reason,
                    }
                finally:
                    app2.db.close()
                if proc.poll() is None:
                    proc.kill()
            except Exception as exc:
                evidence["scenarios"]["restart_recovery"] = {
                    "error": repr(exc)}

            # 场景 4：组合恢复——真实 RTO/RPO 与身份保持
            try:
                app.db.save_task(Task(user_goal="post-backup change"))
                incident = time.time()
                report = CompositeRecovery(
                    str(data_dir), db=app.db, bus=app.bus,
                    memory=app.memory).restore(backup["name"],
                                               incident_at=incident)
                evidence["scenarios"]["composite_recovery"] = {
                    "ok": report["ok"],
                    "identity_preserved": report["identity_preserved"],
                    "counts_matched": report["counts_matched"],
                    "rto_s": report["rto_s"],
                    "rpo_s": report["rpo_s"],
                    "loss_window_s": report["data_loss_window"]["seconds"],
                    "counts_after_tasks": report["counts_after"]["tasks"],
                    "counts_after_events": report["counts_after"]["events"],
                }
            except Exception as exc:
                evidence["scenarios"]["composite_recovery"] = {
                    "error": repr(exc)}

            # 场景 5：单一权威——恢复后无重复任务行、事件数回到备份点
            try:
                ids = [r[0] for r in app.db._conn.execute(
                    "SELECT task_id FROM tasks").fetchall()]
                composite = evidence["scenarios"]["composite_recovery"]
                evidence["scenarios"]["single_authority"] = {
                    "task_rows": len(ids),
                    "task_ids_unique": len(ids) == len(set(ids)),
                    "counts_after_tasks": composite.get("counts_after_tasks"),
                    "counts_after_events": composite.get("counts_after_events"),
                }
            except Exception as exc:
                evidence["scenarios"]["single_authority"] = {
                    "error": repr(exc)}
        finally:
            app.db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
        print(f"\n证据已写入: {out_path}")
    return 0


class _Store:
    """最小安装存储（探针用）。"""

    def __init__(self):
        self.installations = {}

    def get_installation(self, installation_id):
        return self.installations.get(installation_id)

    def save_installation(self, installation_id, device_id, platform,
                          app_version, current_release_id, target_release_id,
                          status, data):
        self.installations[installation_id] = {
            "installation_id": installation_id, "device_id": device_id,
            "platform": platform, "app_version": app_version,
            "current_release_id": current_release_id,
            "target_release_id": target_release_id, "status": status,
            "data": data}

    def get_device_deployment(self, deployment_id):
        return None

    def save_device_deployment(self, deployment_id, installation_id,
                               release_id, action, outcome, data):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
