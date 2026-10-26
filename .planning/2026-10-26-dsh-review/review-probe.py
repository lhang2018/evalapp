"""dsh-lead 独立复核探针：ALN-25-B（主动强度/停止/资源非干扰）与 ALN-30-A（装配收口/可达性）。

只读/临时目录，不改被审代码；夹具明确标注。复核结论与证据写入
.planning/2026-10-26-dsh-review/。
"""

import io
import json
import os
import tempfile
import time
from contextlib import redirect_stdout
from types import SimpleNamespace

from evoagent.app import App
from evoagent.goals.proactivity import (NO_PROGRESS_LIMIT, ProactivityError,
                                        ProactivityGovernor, level_policy)
from evoagent.integration.wiring import (WiringError, assert_wiring_complete,
                                         audit_wiring)
from evoagent.core.task import Task, TaskStatus


class FakeLedger:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def check(self, scope, max_model_calls=None):
        self.calls.append({"scope": scope, "max_model_calls": max_model_calls})
        if self.fail:
            raise RuntimeError("主动预算池已尽(夹具)")


class FakeResource:
    def __init__(self, decision="admit", reason="ok", snapshot=None):
        self.decision = decision
        self.reason = reason
        self.snapshot = {"sample_valid": True} if snapshot is None else snapshot

    def admit(self, kind):
        return SimpleNamespace(decision=self.decision, reason=self.reason,
                               snapshot=self.snapshot, kind=kind)


def probe_25b():
    out = {}

    # 1) 四档与 off 即停
    levels = {lvl: level_policy(lvl) for lvl in ("off", "low", "normal", "high")}
    off = ProactivityGovernor(level="off")
    off_decision = off.may_start()
    normal = ProactivityGovernor(level="normal")
    normal_decision = normal.may_start()
    invalid = None
    try:
        ProactivityGovernor(level="turbo")
    except ProactivityError as exc:
        invalid = str(exc)
    out["levels"] = {k: {"may_start": v["may_start"],
                         "max_concurrent": v["max_concurrent"],
                         "max_model_calls": v["max_model_calls"],
                         "no_progress_limit": NO_PROGRESS_LIMIT.get(k, 0)}
                     for k, v in levels.items()}
    out["off_blocks_with_notice"] = (off_decision.allowed is False
                                     and bool(off_decision.safety_notice))
    out["normal_allows"] = normal_decision.allowed is True
    out["invalid_level_rejected"] = invalid is not None

    # 2) 独立预算池：只查 proactive scope，核心预算不被触碰
    ledger = FakeLedger(fail=False)
    governor = ProactivityGovernor(level="normal", ledger=ledger)
    allowed = governor.may_start()
    ledger_fail = FakeLedger(fail=True)
    denied = ProactivityGovernor(level="normal", ledger=ledger_fail).may_start()
    out["proactive_budget_scope"] = ledger.calls
    out["budget_exhausted_denies"] = (denied.allowed is False
                                      and any("主动预算" in r for r in denied.reasons))
    out["budget_scope_is_proactive"] = all(
        c["scope"] == "proactive" for c in ledger.calls)

    # 3) 无进展停止 + 用户重置恢复
    streak = ProactivityGovernor(level="normal")
    limit = NO_PROGRESS_LIMIT["normal"]
    stopped_after = None
    for index in range(limit + 1):
        streak.record_attempt(had_benefit=False)
        if not streak.may_start().allowed:
            stopped_after = index + 1
            break
    before_reset = streak.may_start()
    streak.reset_no_progress()
    after_reset = streak.may_start()
    out["no_progress_stop"] = {
        "limit": limit, "stopped_after": stopped_after,
        "blocked_before_reset": before_reset.allowed is False,
        "allowed_after_reset": after_reset.allowed is True,
        "streak_after_reset": streak.status().get("no_progress_streak")}

    # 4) 资源非干扰：高压让位、未知采样拒绝、决策器异常拒绝
    high = ProactivityGovernor(
        level="normal",
        resource_governor=FakeResource("defer", "cpu_high"))
    high_decision = high.may_start()
    unknown = ProactivityGovernor(
        level="normal",
        resource_governor=FakeResource("admit", snapshot={"sample_valid": False}))
    unknown_decision = unknown.may_start()

    class Boom:
        def admit(self, kind):
            raise RuntimeError("采样器故障")

    down = ProactivityGovernor(level="normal", resource_governor=Boom())
    out["resource_yield"] = {
        "high_pressure_denied": high_decision.allowed is False,
        "high_reason": high_decision.reasons,
        "invalid_sample_denied": unknown_decision.allowed is False,
        "invalid_sample_reason": unknown_decision.reasons,
        "governor_error_denied": down.may_start().allowed is False,
    }

    # 5) 主动失败隔离：只记策略状态，不写账本
    fail_ledger = FakeLedger()
    fail_governor = ProactivityGovernor(level="normal", ledger=fail_ledger)
    before_calls = len(fail_ledger.calls)
    failure = fail_governor.handle_failure("夹具失败")
    after_calls = len(fail_ledger.calls)
    out["failure_isolation"] = {
        "result": failure, "ledger_calls_unchanged": before_calls == after_calls,
        "core_state_touched": failure.get("core_state_touched")}

    # 6) 档位切换即时生效
    switch = ProactivityGovernor(level="low")
    switched = switch.set_level("high")
    out["level_switch"] = switched
    return out


def probe_30a(tmp_path):
    app = App(data_dir=str(tmp_path))
    out = {}
    try:
        attrs = ["identity_registry", "deployment", "recovery",
                 "credentials", "knowledge", "memory_control", "assist",
                 "delivery"]
        out["app_attrs"] = {name: hasattr(app, name) for name in attrs}
        out["identity_injected"] = (
            getattr(app.delivery, "identity_registry", None) is app.identity_registry)
        out["assist_uses_memory_control"] = (
            getattr(app.assist, "memory_control", None) is app.memory_control)

        report = audit_wiring(app)
        out["audit"] = {"ok": report["ok"], "total": report["total"],
                        "wired": report["wired"],
                        "dangling": [d["capability"] for d in report["dangling"]]}
        try:
            assert_wiring_complete(app)
            out["assert_complete"] = "ok"
        except WiringError as exc:
            out["assert_complete"] = "RAISED: " + str(exc)

        class MissingApp:
            pass

        from evoagent.integration.wiring import Capability
        orphan = Capability(name="复核悬空探针", domain="review",
                            card="ALN-30-A", app_attr="definitely_missing")
        broken = audit_wiring(MissingApp(), capabilities=(orphan,))
        raised = None
        try:
            assert_wiring_complete(MissingApp(), capabilities=(orphan,))
        except WiringError as exc:
            raised = str(exc)
        out["missing_entry_detected"] = {
            "ok": broken["ok"],
            "dangling_count": len(broken["dangling"]),
            "raises": bool(raised),
        }

        # CLI 真实路径（进程内调用 + 重定向 stdout；临时数据目录）
        from evoagent import cli
        cli._app = lambda demo_confirm=False: app

        def run(fn, *args):
            buf = io.StringIO()
            with redirect_stdout(buf):
                fn(*args)
            text = buf.getvalue().strip()
            try:
                return json.loads(text)
            except ValueError:
                return {"raw": text}

        deploy_target = os.path.join(tmp_path, "deploy_target")
        deploy = run(cli.cmd_host_deploy, deploy_target, "0.1.0", "review-probe")
        backup = app.backups.create("reviewprobe.zip")
        restore = run(cli.cmd_host_restore, "")
        credential = run(cli.cmd_credential_audit, "-----BEGIN PRIVATE KEY-----")
        wiring = run(cli.cmd_wiring_audit)
        out["cli_host_deploy"] = {"has_manifest": bool(deploy.get("manifest")),
                                  "verify": deploy.get("verify")}
        out["cli_backup"] = {"name": backup.get("name")}
        out["cli_host_restore_without_deployed_instance"] = restore
        out["cli_host_restore_fail_closed"] = bool(restore.get("error"))
        out["cli_credential_audit"] = credential.get("text_audit")
        out["cli_wiring_audit"] = {"ok": wiring.get("ok"),
                                   "dangling": [d["capability"] for d in wiring.get("dangling", [])]}

        # 未结束任务时恢复必须被拒（LR-01 修复点）
        running = Task(user_goal="review unfinished", task_status=TaskStatus.RUNNING)
        app.db.save_task(running)
        refused = run(cli.cmd_host_restore, "")
        out["restore_with_unfinished_task"] = refused
        app.db._conn.execute("DELETE FROM tasks WHERE task_id=?",
                             (running.task_id,))
        app.db._conn.commit()
    finally:
        app.db.close()
    return out


def main():
    tmp = tempfile.mkdtemp(prefix="dsh_review_")
    proactivity = probe_25b()
    wiring = probe_30a(os.path.join(tmp, "wiring_app"))
    checks = {
        "off_blocks_with_safety_notice": proactivity["off_blocks_with_notice"],
        "normal_allows": proactivity["normal_allows"],
        "invalid_level_rejected": proactivity["invalid_level_rejected"],
        "proactive_budget_isolated": proactivity["budget_scope_is_proactive"]
        and proactivity["budget_exhausted_denies"],
        "no_progress_stop_and_reset": proactivity["no_progress_stop"]["blocked_before_reset"]
        and proactivity["no_progress_stop"]["allowed_after_reset"],
        "resource_pressure_yields": proactivity["resource_yield"]["high_pressure_denied"],
        "invalid_resource_sample_denied": proactivity["resource_yield"]["invalid_sample_denied"],
        "resource_governor_error_denied": proactivity["resource_yield"]["governor_error_denied"],
        "failure_isolated": proactivity["failure_isolation"]["ledger_calls_unchanged"]
        and proactivity["failure_isolation"]["core_state_touched"] is False,
        "app_attrs_present": all(proactivity and wiring["app_attrs"].values()),
        "identity_injected": wiring["identity_injected"],
        "wiring_audit_zero_dangling": wiring["audit"]["ok"] is True
        and not wiring["audit"]["dangling"],
        "wiring_assert_ok": wiring["assert_complete"] == "ok",
        "missing_entry_detected": wiring["missing_entry_detected"]["raises"] is True,
        "cli_host_deploy_real": wiring["cli_host_deploy"]["has_manifest"] is True,
        "restore_fail_closed_without_deployment": wiring["cli_host_restore_fail_closed"],
        "cli_wiring_audit_zero_dangling": wiring["cli_wiring_audit"]["ok"] is True,
        "credential_fragment_detected": wiring["cli_credential_audit"]
        == {"contains_secret": True},
        "restore_refused_with_unfinished_task":
            bool(wiring["restore_with_unfinished_task"].get("error")),
    }
    evidence = {
        "reviewer": "dsh-lead（独立复核；用户要求后续再由另一审阅人复核一遍）",
        "date": "2026-10-26",
        "cards": ["ALN-25-B", "ALN-30-A"],
        "proactivity_25B": proactivity,
        "wiring_30A": wiring,
        "checks": checks,
        "all_checks_pass": all(checks.values()),
        "boundary": ("仅临时目录与夹具；未做真实设备/生产资源压力实测；"
                     "25-B 的资源压力让位为夹具决策器（非 15-D 真实采样）；"
                     "30-A 未验证客户端/生产部署。"),
    }
    path = r"E:\evalapp\.planning\2026-10-26-dsh-review\review-probes.json"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(evidence, handle, ensure_ascii=False, indent=1, default=str)
    print(json.dumps({"checks": checks, "all": evidence["all_checks_pass"]},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
