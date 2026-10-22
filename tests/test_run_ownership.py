import json
import threading
import time
import urllib.error
import urllib.request
import pytest

from evoagent.app import App
from evoagent.core.leases import CURRENT_LEASE, LeaseLost
from evoagent.core.persistence import Database
from evoagent.core.runs import RunManager
from evoagent.core.task import Task, TaskStatus


def abandon(manager, task):
    rid, started = manager.begin(task, {})
    token, reset, stop, thread = manager._handles.pop(rid)
    stop.set(); thread.join(2); CURRENT_LEASE.reset(reset)
    manager.db._conn.execute("UPDATE run_leases SET expires_at=0 WHERE task_id=?", (task.task_id,))
    manager.db._conn.commit()
    return token, rid, started


def test_stale_owner_cannot_overwrite_checkpoints_runs_or_execute_tools(tmp_path):
    db = Database(str(tmp_path / "test.db")); other = Database(db.path)
    first, second = RunManager(db), RunManager(other)
    task = Task(); old, rid, started = abandon(first, task)
    task.task_status = TaskStatus.RUNNING; db.save_task(task)
    db.save_checkpoint("cp", task.task_id, time.time(), {"budget_state": {"usage": {"model_calls": 1}}})
    current = other.get_task(task.task_id)
    newrid, newstart = second.begin(current, {}, recovery=True)
    new_context = CURRENT_LEASE.get()
    CURRENT_LEASE.set(old)
    try:
        from evoagent.tools.registry import ToolRegistry
        from evoagent.tools.contract import ToolContract
        tools = ToolRegistry(); called = []
        tools.register(ToolContract(tool_id="test"), lambda args: called.append(args))
        writes = [lambda: db.save_task(task),
                  lambda: db.save_checkpoint("stale", task.task_id, time.time(), {}),
                  lambda: db.save_run(rid, task.task_id, started, time.time(), "SUCCEEDED", {}),
                  lambda: db.save_tool_execution("old", "test", {}),
                  lambda: tools.execute("test", {})]
        for write in writes:
            with pytest.raises(LeaseLost): write()
        assert not called
        first.leases.release(old)
        row = other._conn.execute("SELECT * FROM run_leases").fetchone()
        assert row["active"] and row["generation"] == 2
    finally:
        CURRENT_LEASE.set(new_context); second.finish(current, newrid, newstart)
        db.close(); other.close()


@pytest.mark.parametrize("status", [TaskStatus.WAITING_USER, TaskStatus.SUCCEEDED, TaskStatus.CANCELLED])
def test_killed_after_task_saved_only_finishes_old_run(tmp_path, status):
    app = App(data_dir=str(tmp_path))
    try:
        task = Task(); token, rid, _ = abandon(app.controller.runs, task)
        task.task_status = status
        task.waiting_request = {"request_id": "still-here"}
        app.db.save_task(task)
        recovered = app.controller.recover_task(task.task_id)
        assert recovered.task_status == status
        assert recovered.waiting_request == task.waiting_request
        assert recovered.current_run_id == rid
        assert len(app.db.list_runs(task.task_id)) == 1
        assert app.db.list_runs(task.task_id)[0]["outcome"] == (status.value if task.is_terminal() else "INTERRUPTED")
    finally: app.db.close()


def test_missing_budget_checkpoint_never_starts_speculative_run(tmp_path):
    app = App(data_dir=str(tmp_path))
    try:
        task = Task(); abandon(app.controller.runs, task)
        task.task_status = TaskStatus.RUNNING; app.db.save_task(task)
        result = app.controller.recover_task(task.task_id)
        assert result.task_status == TaskStatus.SUSPENDED
        assert "检查点" in result.runtime_manifest["recovery_blocked"]
        with pytest.raises(ValueError, match="检查点"): app.controller.resume_task(task.task_id, force=True)
        assert len(app.db.list_runs(task.task_id)) == 1
    finally: app.db.close()


def test_old_database_without_lease_is_readable_but_not_assumed_dead(tmp_path):
    app = App(data_dir=str(tmp_path))
    try:
        task = Task(task_status=TaskStatus.RUNNING); app.db.save_task(task)
        with pytest.raises(ValueError, match="所有权"): app.controller.recover_task(task.task_id)
        assert app.db.get_task(task.task_id).task_status == TaskStatus.RUNNING
    finally: app.db.close()


def test_expired_worker_returning_after_takeover_cannot_publish_old_answer(tmp_path):
    from evoagent.models.base import Action
    from evoagent.models.mock import MockModel
    first = App(data_dir=str(tmp_path)); second = App(data_dir=str(tmp_path))
    ready, unblock = threading.Event(), threading.Event()
    class OldModel(MockModel):
        def suggest_action(self, messages, tools):
            ready.set(); unblock.wait(10)
            return Action(kind="finish", answer="stale answer")
    class NewModel(MockModel):
        def suggest_action(self, messages, tools): return Action(kind="finish", answer="new answer")
    first.controller.router.route = lambda *a, **k: OldModel()
    second.controller.router.route = lambda *a, **k: NewModel()
    first.controller.runs.leases.ttl = 0.3
    returned = []
    worker = threading.Thread(target=lambda: returned.append(first.submit("现在几点了", intent="time_query")))
    worker.start()
    try:
        assert ready.wait(5)
        task = first.db.list_tasks()[0]
        handle = first.controller.runs._handles[task.current_run_id]
        handle[2].set(); handle[3].join(2)
        time.sleep(0.4)
        recovered = second.controller.recover_task(task.task_id)
        assert recovered.task_status == TaskStatus.SUCCEEDED
        before = second.db.get_task(task.task_id).to_dict()
        unblock.set(); worker.join(5)
        assert not worker.is_alive()
        assert returned[0].result == "new answer"
        assert second.db.get_task(task.task_id).to_dict() == before
        assert [r["outcome"] for r in second.db.list_runs(task.task_id)] == ["INTERRUPTED", "SUCCEEDED"]
    finally:
        unblock.set(); worker.join(5); first.db.close(); second.db.close()


def test_admin_recover_endpoint_rejects_live_owner_and_unprivileged_calls(tmp_path):
    from evoagent.api.server import ApiServer
    app = App(data_dir=str(tmp_path))
    task = Task(user_goal="现在几点了", intent="time_query", domain="general")
    abandon(app.controller.runs, task)
    task.task_status = TaskStatus.RUNNING; app.db.save_task(task)
    app.db.save_checkpoint("cp", task.task_id, time.time(), {"budget_state": {"usage": {"model_calls": 1}}})
    app.config.api_token, app.config.api_admin_token = "operator", "admin"
    server = ApiServer(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    def request(task_id, token):
        req = urllib.request.Request(f"http://127.0.0.1:{server.port}/tasks/{task_id}/recover", data=b"{}",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        try: response = urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as error: response = error
        with response: return response.status, json.loads(response.read())
    try:
        assert request(task.task_id, "")[0] == 401
        assert request(task.task_id, "invalid")[0] == 403
        assert request(task.task_id, "operator")[0] == 403
        assert request("missing", "admin")[0] == 404
        app.db._conn.execute("UPDATE run_leases SET expires_at=?", (time.time() + 30,)); app.db._conn.commit()
        assert request(task.task_id, "admin")[0] == 409
        app.db._conn.execute("UPDATE run_leases SET expires_at=0"); app.db._conn.commit()
        assert request(task.task_id, "admin")[0] == 202
        deadline = time.monotonic() + 5
        while task.task_id in app.controller._active and time.monotonic() < deadline: time.sleep(0.01)
        assert len(app.db.list_runs(task.task_id)) == 2
    finally:
        server.shutdown(); thread.join(5); app.db.close()
