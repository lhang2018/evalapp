"""Feedback aggregation must retain bounded evidence and valid timestamps."""
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

from evoagent.api.feedback_api import submit_feedback
from evoagent.core.persistence import Database
from evoagent.core.task import Task
from evoagent.observability.feedback import FeedbackError, FeedbackStore


@pytest.fixture
def database(tmp_path):
    db = Database(str(tmp_path / "feedback.db"))
    yield db
    db.close()


def store(db):
    return FeedbackStore(db._conn, lock=db._lock)


def test_aggregate_retains_new_evidence_and_deduplicates_in_order(database):
    feedback = store(database)
    first = feedback.submit(category="bug", text="same", evidence_refs=["evt_a", "evt_a"], now=100)
    second = feedback.submit(category="bug", text="same", evidence_refs=["evt_b", "evt_a"], now=101)
    row = feedback.get(first["feedback_id"])
    assert first["feedback_id"] == second["feedback_id"]
    assert second["count"] == 2 and row["cause_status"] == "unverified"
    assert row["evidence_refs"] == ["evt_a", "evt_b"]


def test_evidence_bound_rejection_is_atomic_and_does_not_claim_acceptance(database):
    feedback = store(database)
    first = feedback.submit(category="bug", text="same", evidence_refs=[f"evt_{i}" for i in range(32)])
    before = feedback.get(first["feedback_id"])
    with pytest.raises(FeedbackError, match="32"):
        feedback.submit(category="bug", text="same", evidence_refs=["evt_overflow"])
    assert feedback.get(first["feedback_id"]) == before


def test_two_connections_merge_evidence_without_lost_updates(database):
    peer = Database(database.path)
    gate = threading.Barrier(2)
    def submit(db, prefix):
        gate.wait(timeout=5)
        return [store(db).submit(category="bug", text="concurrent",
            evidence_refs=[f"evt_{prefix}_{i}"]) for i in range(8)]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(submit, database, "a")
            b = pool.submit(submit, peer, "b")
            results = a.result(timeout=15) + b.result(timeout=15)
        assert len({r["feedback_id"] for r in results}) == 1
        row = store(database).get(results[0]["feedback_id"])
        assert row["count"] == 16
        assert set(row["evidence_refs"]) == {f"evt_{p}_{i}" for p in "ab" for i in range(8)}
    finally:
        peer.close()


@pytest.mark.parametrize("now", [float("nan"), float("inf"), -float("inf"), -1, True, "yesterday"])
def test_invalid_clock_is_rejected_before_writing(database, now):
    with pytest.raises(FeedbackError):
        store(database).submit(category="bug", text="bad time", now=now)
    assert database._conn.execute("SELECT count(*) FROM user_feedback").fetchone()[0] == 0


def test_clock_regression_does_not_move_aggregate_back_in_time(database):
    feedback = store(database)
    first = feedback.submit(category="bug", text="same", now=100)
    feedback.submit(category="bug", text="same", now=90)
    row = feedback.get(first["feedback_id"])
    assert row["first_seen_at"] == row["last_seen_at"] == 100


@pytest.mark.parametrize("bad", ["not json", "{}", '"evt_a"'])
def test_corrupt_existing_evidence_is_not_silently_overwritten(database, bad):
    feedback = store(database)
    first = feedback.submit(category="bug", text="same", evidence_refs=["evt_a"])
    with database._conn:
        database._conn.execute("UPDATE user_feedback SET evidence_refs=? WHERE feedback_id=?",
                               (bad, first["feedback_id"]))
    with pytest.raises(FeedbackError):
        feedback.submit(category="bug", text="same", evidence_refs=["evt_b"])
    assert database._conn.execute("SELECT count FROM user_feedback").fetchone()[0] == 1


@pytest.mark.parametrize("manifest", ["broken-json", "[]", '"string"', "null"])
def test_invalid_run_manifest_returns_conflict_without_guessing_or_writing(database, manifest):
    task = Task(task_id="feedback_task", user_goal="review")
    database.save_task(task)
    database.save_run("feedback_run", task.task_id, 1, 2, "SUCCEEDED", {})
    with database._conn:
        database._conn.execute("UPDATE task_runs SET version_manifest=? WHERE run_id=?",
                               (manifest, "feedback_run"))
    ctx = SimpleNamespace(app=SimpleNamespace(db=database),
        body={"category": "bug", "text": "run issue", "run_id": "feedback_run"},
        json=lambda status, payload: (status, payload))
    status, payload = submit_feedback(ctx)
    assert status == 409 and payload["error"]
    assert database._conn.execute("SELECT count(*) FROM user_feedback").fetchone()[0] == 0


def test_valid_run_uses_its_snapshot_even_when_latest_task_manifest_changed(database):
    task = Task(task_id="feedback_task", user_goal="review")
    task.runtime_manifest["runtime_manifest_id"] = "manifest_new"
    database.save_task(task)
    database.save_run("feedback_run", task.task_id, 1, 2, "SUCCEEDED",
                      {"runtime_manifest_id": "manifest_old"})
    ctx = SimpleNamespace(app=SimpleNamespace(db=database),
        body={"category": "bug", "text": "run issue", "run_id": "feedback_run"},
        json=lambda status, payload: (status, payload))
    status, result = submit_feedback(ctx)
    assert status == 201
    assert store(database).get(result["feedback_id"])["manifest_id"] == "manifest_old"


def test_sql_failure_rolls_back_count_and_evidence_together(database):
    import sqlite3
    feedback = store(database)
    first = feedback.submit(category="bug", text="same", evidence_refs=["evt_a"])
    before = feedback.get(first["feedback_id"])
    with database._conn:
        database._conn.execute("CREATE TRIGGER feedback_test_abort BEFORE UPDATE ON user_feedback "
                               "BEGIN SELECT RAISE(ABORT, 'controlled write failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="controlled write failure"):
        feedback.submit(category="bug", text="same", evidence_refs=["evt_b"])
    assert feedback.get(first["feedback_id"]) == before
    assert database._conn.in_transaction is False


def test_existing_caller_transaction_is_neither_committed_nor_rolled_back(database):
    with database._conn:
        database._conn.execute("CREATE TABLE feedback_test_unrelated(value INTEGER)")
    database._conn.execute("INSERT INTO feedback_test_unrelated VALUES (1)")
    with pytest.raises(FeedbackError, match="在途事务"):
        store(database).submit(category="bug", text="same", evidence_refs=["evt_a"])
    assert database._conn.in_transaction is True
    database._conn.rollback()
    assert database._conn.execute("SELECT count(*) FROM feedback_test_unrelated").fetchone()[0] == 0


def test_feedback_http_audit_reads_really_require_admin(tmp_path):
    import json
    import urllib.error
    import urllib.request
    from evoagent.api.server import ApiServer
    from evoagent.app import App
    app = App(data_dir=str(tmp_path))
    app.config.api_token = "review-local-operator"
    app.config.api_admin_token = "review-local-admin"
    server = ApiServer(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(path, token, payload=None):
        req = urllib.request.Request(f"http://127.0.0.1:{server.port}" + path,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            data=json.dumps(payload).encode() if payload is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())
    try:
        status, row = request("/feedback", app.config.api_token,
                              {"category": "bug", "text": "local HTTP permission fixture"})
        assert status == 201
        path = "/feedback/" + row["feedback_id"]
        assert request("/feedback", app.config.api_token)[0] == 403
        assert request(path, app.config.api_token)[0] == 403
        assert request(path, app.config.api_admin_token)[0] == 200
    finally:
        server.shutdown()
        thread.join(timeout=5)
        app.memory.close()
        app.db.close()
