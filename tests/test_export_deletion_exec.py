"""导出下载与删除计划的实际执行（ALN-16-X）回归。

夹具先证缺口：`export_to_file` / `execute_deletion` 不存在即 import 失败。
覆盖任务卡 §验收 1-5：删除生效可查、fail-closed、幂等、文件审计与路径逃逸、确定性。
"""
import json
import os

import pytest

from evoagent.observability.contract import EvidenceEvent, EventRefs
from evoagent.observability.export import (DeletionPlan, DeletionScopeError,
                                           EvidenceExporter, ExportDenied,
                                           execute_deletion, export_to_file,
                                           plan_deletion)


def _event(eid, task_id="t1", level="internal"):
    return EvidenceEvent(
        event_id=eid, event_type="tool.call", timestamp=1000.0 + len(eid),
        severity="info", privacy_level=level, fault_level="none",
        origin="server", trust="trusted",
        refs=EventRefs(task_id=task_id, run_id="r1"))


def _events():
    return [_event("e1"), _event("e2"), _event("other", task_id="t2")]


def _deleter():
    """内存版逐层删除器：记录每层删除数，可重复执行（模拟幂等）。"""
    class D:
        def __init__(self):
            self.calls = 0

        def delete_task(self, task_id):
            self.calls += 1
            n = 2 if self.calls == 1 else 0   # 第二次已删光 → 全 0（幂等）
            return {"runtime_event": n, "execution_trace": 0,
                    "diagnostic_evidence": 0, "diagnostics": n}
    return D()


# ---- 验收 1：计划→执行→审计 ----
def test_plan_execute_tombstone_complete():
    events = _events()
    plan = plan_deletion(events, task_id="t1", actor="admin",
                         reason="用户请求删除")
    assert plan.event_ids == ("e1", "e2")
    d = _deleter()
    out = execute_deletion(d, plan, actor="admin", reason="执行删除计划")
    assert out["ok"] is True
    assert out["total"] >= 2
    ts = out["tombstone"]
    assert ts["executed_by"] == "admin"
    assert ts["execute_reason"] == "执行删除计划"
    assert ts["executed_counts"]
    assert ts["digest"] == plan.digest


# ---- 验收 2：fail-closed ----
def test_digest_mismatch_rejected():
    plan = plan_deletion(_events(), task_id="t1", actor="admin", reason="r")
    tampered = DeletionPlan(scope=plan.scope, actor=plan.actor,
                            reason=plan.reason,
                            event_ids=plan.event_ids + ("e99",),
                            digest=plan.digest, tombstone=plan.tombstone)
    d = _deleter()
    with pytest.raises(DeletionScopeError):
        execute_deletion(d, tampered, actor="admin", reason="r")
    assert d.calls == 0  # 未删除任何数据


def test_verify_can_be_disabled_but_ids_still_scope():
    plan = plan_deletion(_events(), task_id="t1", actor="admin", reason="r")
    d = _deleter()
    out = execute_deletion(d, plan, actor="admin", reason="r",
                           verify_digest=False)
    assert out["ok"] is True


def test_missing_actor_or_reason_rejected():
    plan = plan_deletion(_events(), task_id="t1", actor="admin", reason="r")
    with pytest.raises(DeletionScopeError):
        execute_deletion(_deleter(), plan, actor="", reason="r")
    with pytest.raises(DeletionScopeError):
        execute_deletion(_deleter(), plan, actor="admin", reason="")


def test_empty_scope_rejected():
    plan = DeletionPlan(scope="", actor="a", reason="r", event_ids=(),
                        digest="x", tombstone={})
    with pytest.raises(DeletionScopeError):
        execute_deletion(_deleter(), plan, actor="admin", reason="r")


# ---- 验收 3：幂等 ----
def test_idempotent_replay():
    plan = plan_deletion(_events(), task_id="t1", actor="admin", reason="r")
    d = _deleter()
    first = execute_deletion(d, plan, actor="admin", reason="r")
    second = execute_deletion(d, plan, actor="admin", reason="重放")
    assert first["total"] >= 2
    assert second["total"] == 0          # 已删光：全 0
    assert second["ok"] is True          # 不算失败


# ---- 验收 4：导出文件 + 审计头 + 路径逃逸 ----
def test_export_to_file_with_audit(tmp_path):
    exp = EvidenceExporter()
    out = export_to_file(exp, _events(), "exports/report.json",
                         actor="admin", reason="审计导出", task_id="t1",
                         allowed_root=str(tmp_path))
    assert os.path.isfile(out["file"])
    body = json.load(open(out["file"], encoding="utf-8"))
    assert body["events_digest"] == out["audit"]["events_digest"]
    assert body["actor"] == "admin" and body["reason"] == "审计导出"
    assert out["audit"]["count"] == 2
    assert len(out["sha256"]) == 64


def test_export_path_escape_rejected(tmp_path):
    exp = EvidenceExporter()
    with pytest.raises(ExportDenied):
        export_to_file(exp, _events(), "../outside.json",
                       actor="admin", reason="r", allowed_root=str(tmp_path))


def test_export_symlink_escape_rejected(tmp_path):
    outside = tmp_path.parent / "outside-target.json"
    outside.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        os.symlink(outside, link)
    except OSError:
        return  # 平台不支持 symlink 时跳过
    exp = EvidenceExporter()
    with pytest.raises(ExportDenied):
        export_to_file(exp, _events(), "link.json", actor="admin",
                       reason="r", allowed_root=str(tmp_path))


# ---- 验收 5：确定性（digest 口径不变） ----
def test_plan_deterministic():
    p1 = plan_deletion(_events(), task_id="t1", actor="a", reason="r")
    p2 = plan_deletion(_events(), task_id="t1", actor="a", reason="r")
    assert p1.event_ids == p2.event_ids and p1.digest == p2.digest
