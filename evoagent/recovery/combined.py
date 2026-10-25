"""组合恢复与 RTO/RPO 测量（ALN-17-P）。

复用 `core/backup.py` 已接受的备份/校验/恢复能力（不重写），补齐三件事：

1. **空机 → 组合资产恢复**：目标实例必须先通过 `deployment.verify`（结构/
   schema/身份），才允许恢复；备份的 schema 高于当前程序 → **不兼容拒绝**，
   且在动任何数据之前判断；
2. **组合恢复**：数据三库（任务/发布/审计、事件、记忆）+ 部署身份 + 行数
   快照一起核对；恢复后实例身份必须保持（不被备份内容覆盖），行数与备份
   清单逐项对齐；
3. **真实时间与损失窗口**：RTO = 恢复实际耗时（墙钟），RPO/损失窗口 =
   事故时刻 − 备份创建时刻；恢复失败时自动从安全备份回退，并记录回退耗时
   与回退后行数。目标值是本卡配置的验收界，不是生产 SLA 承诺。
"""

import argparse
import json
import shutil
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..core.backup import MANIFEST_NAME, BackupManager
from ..core.schema.migrations import SCHEMA_VERSION, current_version
from ..deployment.bootstrap import DeploymentError, load, verify


class IncompatibleBackup(RuntimeError):
    """备份与当前程序 schema 不兼容（拒绝恢复，不动数据）。"""


@dataclass(frozen=True)
class RecoveryPolicy:
    rto_target_s: float = 300.0
    rpo_target_s: float = 600.0
    take_safety_backup: bool = True


class CompositeRecovery:
    """组合恢复编排：兼容判定 → 安全备份 → 恢复 → 计时与对账。"""

    def __init__(self, data_dir: str, *, db: Any, bus: Any, memory: Any,
                 releases: Any = None, policy: Any = None, clock=None):
        self.data_dir = Path(data_dir)
        self.db, self.bus, self.memory = db, bus, memory
        self.manager = BackupManager(str(self.data_dir), db=db, bus=bus,
                                     memory=memory, releases=releases)
        self.policy = (policy if isinstance(policy, RecoveryPolicy)
                       else RecoveryPolicy(**policy) if isinstance(policy, dict)
                       else RecoveryPolicy())
        self.clock = clock or time.time

    # ---- 兼容判定（动数据之前）----
    def inspect(self, backup_ref: str) -> Dict[str, Any]:
        check = self.manager.verify(backup_ref)
        if not check.get("ok"):
            raise ValueError("备份校验失败: " + "; ".join(check.get("errors") or []))
        manifest = check.get("manifest") or {}
        target_schema = (current_version(self.db._conn)
                         if self.db is not None else SCHEMA_VERSION)
        backup_schema = int(manifest.get("schema_version") or 0)
        deployment = verify(str(self.data_dir))
        return {
            "backup_name": Path(check["path"]).name,
            "backup_path": check["path"],
            "created_at": float(manifest.get("created_at") or 0.0),
            "backup_schema": backup_schema,
            "target_schema": target_schema,
            "schema_version_source": SCHEMA_VERSION,
            "compatible": backup_schema <= target_schema,
            "will_migrate": backup_schema < target_schema,
            "counts": dict(manifest.get("counts") or {}),
            "release_id": manifest.get("release_id"),
            "deployment_ok": deployment["ok"],
            "deployment_errors": deployment["errors"],
            "instance_id": deployment.get("instance_id"),
        }

    def _guard(self, plan: Dict[str, Any]) -> None:
        if not plan["deployment_ok"]:
            raise DeploymentError(
                "目标实例部署校验未通过，拒绝恢复: "
                + "; ".join(plan["deployment_errors"]))
        if not plan["compatible"]:
            raise IncompatibleBackup(
                f"备份 schema {plan['backup_schema']} 高于当前程序支持 "
                f"{plan['target_schema']}：拒绝恢复（请先升级程序或改用匹配备份）")

    # ---- 组合恢复（成功路径）----
    def restore(self, backup_ref: str, *, incident_at: Optional[float] = None
                ) -> Dict[str, Any]:
        plan = self.inspect(backup_ref)
        self._guard(plan)
        before = self.manager._counts()
        safety = None
        if self.policy.take_safety_backup:
            safety = self.manager.create(
                f"pre_composite_{time.strftime('%Y%m%d-%H%M%S')}_"
                f"{uuid.uuid4().hex[:4]}.zip")["name"]
        started = self.clock()
        try:
            result = self.manager.restore(backup_ref, safety_backup=False)
        except Exception:
            # 失败时由调用方决定是否回退；这里保持异常原样，便于调用方记录
            raise
        finished = self.clock()
        return self._report(plan, result, started, finished, incident_at,
                            safety, before, rollback=None)

    # ---- 组合恢复（失败回退路径）----
    def restore_with_rollback(self, backup_ref: str, *,
                              incident_at: Optional[float] = None,
                              inject_failure: Optional[Callable[[Any], None]] = None
                              ) -> Dict[str, Any]:
        plan = self.inspect(backup_ref)
        self._guard(plan)
        before = self.manager._counts()
        safety = self.manager.create(
            f"pre_composite_{time.strftime('%Y%m%d-%H%M%S')}_"
            f"{uuid.uuid4().hex[:4]}.zip")["name"]
        started = self.clock()
        try:
            if inject_failure is not None:
                inject_failure(self.manager)   # 受控故障注入（明确标注）
            result = self.manager.restore(backup_ref, safety_backup=False)
        except Exception as exc:  # noqa: BLE001 —— 失败必须回退且可复核
            rollback_started = self.clock()
            rollback_ok, rollback_error, rolled = True, None, None
            try:
                rolled = self.manager.restore(safety, safety_backup=False)
            except Exception as rollback_exc:  # noqa: BLE001
                rollback_ok = False
                rollback_error = f"{type(rollback_exc).__name__}: {rollback_exc}"
            rollback_elapsed = self.clock() - rollback_started
            after = self.manager._counts()
            return {
                "ok": False,
                "backup_name": plan["backup_name"],
                "error": f"{type(exc).__name__}: {exc}",
                "safety_backup": safety,
                "identity_preserved": load(str(self.data_dir)).get("instance_id")
                == plan["instance_id"],
                "rollback": {
                    "ok": rollback_ok,
                    "rto_s": round(rollback_elapsed, 3),
                    "error": rollback_error,
                    "restored": rolled,
                    "counts": after,
                    "counts_match_pre_failure": after == before,
                },
                "policy": self._policy_view(),
                "timestamps": {"started_at": started, "failed_at": self.clock()},
            }
        finished = self.clock()
        return self._report(plan, result, started, finished, incident_at,
                            safety, before, rollback=None)

    # ---- 报告与度量 ----
    def _report(self, plan: Dict[str, Any], result: Dict[str, Any],
                started: float, finished: float,
                incident_at: Optional[float], safety: Optional[str],
                before: Dict[str, int],
                rollback: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        rto = finished - started
        incident = finished if incident_at is None else float(incident_at)
        rpo = max(0.0, incident - plan["created_at"])
        expected = plan["counts"]
        after = result.get("counts") or self.manager._counts()
        counts_matched = all(after.get(key) == value
                             for key, value in expected.items()) if expected else True
        identity_after = load(str(self.data_dir)).get("instance_id")
        identity_preserved = identity_after == plan["instance_id"]
        meets = (rto <= self.policy.rto_target_s
                 and rpo <= self.policy.rpo_target_s)
        ok = bool(result.get("ok") and counts_matched and identity_preserved)
        return {
            "ok": ok,
            "backup_name": plan["backup_name"],
            "safety_backup": safety,
            "identity_preserved": identity_preserved,
            "migrations_applied": result.get("migrations_applied") or [],
            "counts_expected": expected,
            "counts_after": after,
            "counts_matched": counts_matched,
            "counts_before_restore": before,
            "rto_s": round(rto, 3),
            "rpo_s": round(rpo, 3),
            "data_loss_window": {"from_backup_at": plan["created_at"],
                                 "to_incident_at": incident,
                                 "seconds": round(rpo, 3)},
            "policy": self._policy_view(meets),
            "timestamps": {"started_at": started, "finished_at": finished},
            "rollback": rollback,
        }

    def _policy_view(self, meets: Optional[bool] = None) -> Dict[str, Any]:
        view = {"rto_target_s": self.policy.rto_target_s,
                "rpo_target_s": self.policy.rpo_target_s,
                "note": "目标值为本卡配置的验收界，不构成生产 SLA 承诺"}
        if meets is not None:
            view["meets_targets"] = meets
        return view

    # ---- 场景矩阵（正/拒绝/回退）----
    def matrix(self, *, backup_ref: str,
               incompatible_ref: Optional[str] = None) -> List[Dict[str, Any]]:
        scenarios: List[Dict[str, Any]] = []
        scenarios.append({"scenario": "compatible_restore",
                          "result": self.restore(backup_ref)})
        if incompatible_ref is not None:
            started = self.clock()
            try:
                self.restore(incompatible_ref)
                refusal = {"rejected": False, "error": "unexpected-success"}
            except IncompatibleBackup as exc:
                refusal = {"rejected": True, "error": str(exc)}
            scenarios.append({"scenario": "incompatible_refusal",
                              "elapsed_s": round(self.clock() - started, 3),
                              "result": refusal})
        scenarios.append({
            "scenario": "failure_rollback",
            "result": self.restore_with_rollback(
                backup_ref,
                inject_failure=lambda _manager: (_ for _ in ()).throw(
                    RuntimeError("受控故障注入：恢复中途失败")))},
        )
        return scenarios


def forge_incompatible_backup(manager: BackupManager, source_ref: str,
                              *, schema_version: int = SCHEMA_VERSION + 5) -> str:
    """复制一份备份并改高 manifest schema 版本（用于不兼容拒绝场景）。"""
    source = manager._resolve(source_ref)
    check = manager.verify(source_ref)
    if not check.get("ok"):
        raise ValueError("源备份校验失败")
    manifest = dict(check.get("manifest") or {})
    manifest["schema_version"] = int(schema_version)
    name = f"forged_schema_{uuid.uuid4().hex[:8]}.zip"
    target = manager.backup_dir / name
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, "w",
                                                        zipfile.ZIP_DEFLATED) as dst:
        for item in src.namelist():
            if item == MANIFEST_NAME:
                continue
            dst.writestr(item, src.read(item))
        dst.writestr(MANIFEST_NAME,
                     json.dumps(manifest, ensure_ascii=False))
    return name


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="组合恢复与 RTO/RPO 实测")
    parser.add_argument("--out", default="")
    parser.add_argument("--rto-target", type=float, default=300.0)
    parser.add_argument("--rpo-target", type=float, default=600.0)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)
    import tempfile
    from ..app import App
    from ..core.task import Task
    from ..deployment.bootstrap import bootstrap

    data_dir = tempfile.mkdtemp(prefix="evoagent_deploy_probe_")
    report: Dict[str, Any] = {"data_dir": data_dir,
                              "bootstrap": None, "matrix": None}
    try:
        manifest = bootstrap(data_dir, app_version="deploy-probe",
                             source_commit="worktree")
        report["bootstrap"] = {key: manifest[key] for key in
                               ("instance_id", "deployed_at", "schema_version_source")}
        app = App(data_dir=data_dir)
        try:
            app.db.save_task(Task(user_goal="deploy probe task"))
            app.bus.emit(event_type="deploy_probe", component_id="recovery",
                         payload={"probe": 1})
            seeded = app.backups.create("seeded_probe.zip")
            # 备份之后制造增量：恢复应回到备份时点
            app.db.save_task(Task(user_goal="post-backup change"))
            app.bus.emit(event_type="post_backup", component_id="recovery",
                         payload={"probe": 2})
            recovery = CompositeRecovery(
                data_dir, db=app.db, bus=app.bus, memory=app.memory,
                releases=app.releases,
                policy=RecoveryPolicy(rto_target_s=args.rto_target,
                                      rpo_target_s=args.rpo_target))
            forged = forge_incompatible_backup(app.backups, seeded["name"])
            report["matrix"] = recovery.matrix(backup_ref=seeded["name"],
                                               incompatible_ref=forged)
            report["policy"] = asdict(recovery.policy)
            report["measured_at"] = time.time()
        finally:
            app.db.close()
    finally:
        if not args.keep:
            shutil.rmtree(data_dir, ignore_errors=True)
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    scenarios = report.get("matrix") or []
    expected = {"compatible_restore": True, "incompatible_refusal": True,
                "failure_rollback": True}
    ok = bool(scenarios) and all(
        (item["scenario"] == "compatible_restore" and item["result"]["ok"])
        or (item["scenario"] == "incompatible_refusal"
            and item["result"]["rejected"])
        or (item["scenario"] == "failure_rollback"
            and item["result"]["rollback"]["ok"]
            and item["result"]["rollback"]["counts_match_pre_failure"])
        for item in scenarios)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
