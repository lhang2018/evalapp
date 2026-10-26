"""Independent negative regressions for the 13-S/09-I/18-H host entries."""
import threading
from types import SimpleNamespace

import pytest

from evoagent.app import App
from evoagent.core.permissions import PermissionDecision, ToolPolicy
from evoagent.experts.coding_consumer import CodingExpertConsumer
from evoagent.goals.scheduler import GoalScheduler
from evoagent.integration.candidate_execution import CandidateEvaluationService
from evoagent.tools.coding import CODING_PACK


def test_stop_timeout_keeps_live_worker_and_prevents_restart(tmp_path):
    app = App(data_dir=str(tmp_path))
    scheduler = app.goal_scheduler
    entered, release = threading.Event(), threading.Event()
    workers = []

    def blocked_tick():
        entered.set()
        assert release.wait(10)

    scheduler.tick = blocked_tick
    try:
        first = scheduler.start(poll_seconds=60)
        workers.append(first)
        assert entered.wait(5)
        scheduler.stop(timeout=0)
        status = scheduler.status()
        # running must describe the actual worker even while stopping.
        assert status["running"] is True
        assert status["stopping"] is True
        again = scheduler.start(poll_seconds=60)
        workers.append(again)
        assert again is first
        assert scheduler._stop.is_set()
        release.set()
        first.join(5)
        assert not first.is_alive()
        scheduler.stop()
        assert scheduler.status()["running"] is False
    finally:
        release.set()
        scheduler.stop()
        for worker in workers:
            worker.join(5)
        app.db.close()


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
def test_start_refuses_invalid_interval_before_starting(interval):
    scheduler = GoalScheduler(SimpleNamespace(db=object(), goals=object()))
    try:
        with pytest.raises(ValueError):
            scheduler.start(poll_seconds=interval)
    finally:
        scheduler.stop(timeout=1)


@pytest.mark.parametrize("mode", ["deny", "confirm"])
def test_project_consumer_obeys_host_permission_policy(tmp_path, mode):
    app = App(data_dir=str(tmp_path / "data"))
    root = tmp_path / "project"
    root.mkdir()
    target = root / "main.py"
    original = "x = 1\n"
    target.write_text(original, encoding="utf-8")
    try:
        app.permissions.high_risk_mode = mode
        app.coding_consumer.install(str(root), expected_version=CODING_PACK.version,
                                    actor="admin", reason="review")
        result = app.coding_consumer.execute(
            str(root), "coding_edit",
            {"path": "main.py", "old_text": "x = 1", "new_text": "x = 2"},
            actor="admin", reason="review")
        assert result["ok"] is False
        assert result["error_class"] == "auth_or_permission"
        assert target.read_text(encoding="utf-8") == original
        # Later policy changes must affect an already installed registry.
        app.permissions.register_tool_policy(ToolPolicy(
            "coding_syntax_check", PermissionDecision.DENY))
        result = app.coding_consumer.execute(
            str(root), "coding_syntax_check", {"targets": ["."]},
            actor="admin", reason="review")
        assert result["ok"] is False
    finally:
        app.db.close()


def test_standalone_consumer_does_not_implicitly_authorize_edits(tmp_path):
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    consumer = CodingExpertConsumer()
    consumer.install(str(tmp_path), expected_version=CODING_PACK.version,
                     actor="admin", reason="review")
    result = consumer.execute(
        str(tmp_path), "coding_edit",
        {"path": "main.py", "old_text": "x = 1", "new_text": "x = 2"},
        actor="admin", reason="review")
    assert result["ok"] is False
    assert (tmp_path / "main.py").read_text(encoding="utf-8") == "x = 1\n"


def test_explicit_host_approval_preserves_two_project_isolation(tmp_path):
    approved = []
    app = App(data_dir=str(tmp_path / "data"), confirm_callback=lambda op, args:
              approved.append((op, dict(args))) is None)
    try:
        for name in ("one", "two"):
            root = tmp_path / name
            root.mkdir()
            (root / "main.py").write_text("x = 1\n", encoding="utf-8")
            app.coding_consumer.install(str(root), expected_version=CODING_PACK.version,
                                        actor="admin", reason="review")
            result = app.coding_consumer.execute(
                str(root), "coding_edit",
                {"path": "main.py", "old_text": "x = 1", "new_text": "x = 2"},
                actor="admin", reason="explicit host confirmation")
            assert result["ok"] is True, result
            assert (root / "main.py").read_text(encoding="utf-8") == "x = 2\n"
        assert len(approved) == 2
        app.permissions.high_risk_mode = "deny"
        result = app.coding_consumer.execute(
            str(tmp_path / "one"), "coding_edit",
            {"path": "main.py", "old_text": "x = 2", "new_text": "x = 3"},
            actor="admin", reason="revoked host permission")
        assert result["ok"] is False
        assert (tmp_path / "one" / "main.py").read_text(encoding="utf-8") == "x = 2\n"
    finally:
        app.db.close()


def test_evaluation_samples_release_before_execution():
    releases = SimpleNamespace(current_release_id="before")
    service = CandidateEvaluationService(object(), releases=releases)
    identity = SimpleNamespace(identity=lambda: {"asset_ref": "test@1.0.0"})
    service.host.resolve = lambda ref: identity

    def compare(*args):
        # Simulate an independent release while evaluation is in progress.
        releases.current_release_id = "after"
        return dict(benchmark_version="probe", timeout_s=5, repeat=2,
                    source="probe", verdict="inconclusive", evidence_digest="probe",
                    release_decision={"allow": False})

    service.benchmark.compare = compare
    result = service.evaluate(candidate_id="test", baseline_ref="base@1.0.0",
                              candidate_ref="cand@1.0.0", cases=[{"case_id": "a"}],
                              actor="admin", reason="review")
    assert result["release_before"] == "before"
    assert result["release_after"] == "after"
    assert result["auto_published"] is False
