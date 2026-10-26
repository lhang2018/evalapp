"""Review actual restore entry points against their existing safety promises."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from evoagent import cli
from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.core.task import Task, TaskStatus
from evoagent.deployment.bootstrap import bootstrap


@pytest.fixture
def app(tmp_path, monkeypatch):
    target = tmp_path / "host"
    bootstrap(str(target), app_version="synthetic-review", source_commit="local-review")
    value = App(data_dir=str(target))
    monkeypatch.setattr(cli, "_app", lambda: value)
    yield value
    value.memory.close()
    value.db.close()


def saved(app, goal, *, status=TaskStatus.SUCCEEDED, at=1.0):
    task = Task(user_goal=goal, task_status=status, created_at=at)
    app.db.save_task(task)
    return task


@pytest.mark.parametrize("status", [TaskStatus.RUNNING, TaskStatus.SUSPENDED,
                                    TaskStatus.WAITING_USER, TaskStatus.CREATED])
def test_new_host_restore_refuses_unfinished_task_before_data_or_backup_changes(app, capsys, status):
    backup = app.backups.create("seed.zip")
    active = saved(app, "synthetic unfinished", status=status)
    before = app.backups._counts()
    before_backups = {entry["name"] for entry in app.backups.list()}
    cli.cmd_host_restore(backup["name"])
    response = json.loads(capsys.readouterr().out)
    assert "error" in response
    assert active.task_id in response.get("active_tasks", [])
    assert app.db.get_task(active.task_id).task_status == status
    assert app.backups._counts() == before
    assert {entry["name"] for entry in app.backups.list()} == before_backups


def fill_recent_terminal_rows(app):
    active = saved(app, "old unfinished", status=TaskStatus.RUNNING, at=1.0)
    # Insert real rows efficiently; the unfinished row is older than the former
    # 1000-row UI/list window. This is not a mocked list_tasks return value.
    rows = []
    for index in range(1001):
        task = Task(task_id=f"terminal-{index}", user_goal="synthetic terminal",
                    task_status=TaskStatus.SUCCEEDED, created_at=10.0+index)
        rows.append((task.task_id, task.task_status.value, task.created_at,
                     json.dumps(task.to_dict())))
    with app.db._lock:
        app.db._conn.executemany("INSERT INTO tasks(task_id,status,created_at,data) VALUES (?,?,?,?)", rows)
        app.db._conn.commit()
    return active


@pytest.mark.parametrize("entry", ["legacy_cli", "http"])
def test_restore_guard_is_not_limited_to_recent_thousand_tasks(app, capsys, entry):
    backup = app.backups.create("seed.zip")
    active = fill_recent_terminal_rows(app)
    before = app.backups._counts()
    if entry == "legacy_cli":
        cli.cmd_backup("restore", backup["name"], "")
        response = json.loads(capsys.readouterr().out)
        assert "error" in response
    else:
        app.config.api_admin_token = "synthetic-review-admin"
        server = ApiServer(app, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.port}/admin/restore",
                headers={"Authorization": "Bearer synthetic-review-admin", "Content-Type": "application/json"},
                data=json.dumps({"name":backup["name"]}).encode())
            try:
                with urllib.request.urlopen(request, timeout=5) as result:
                    status, response = result.status, json.load(result)
            except urllib.error.HTTPError as result:
                status, response = result.code, json.load(result)
            assert status == 409
        finally:
            server.shutdown()
            thread.join(timeout=5)
    assert active.task_id in response.get("active_tasks", [])
    assert app.db.get_task(active.task_id) is not None
    assert app.backups._counts() == before


def test_host_restore_rolls_back_real_data_when_restore_fails_after_replacement(app, monkeypatch, capsys):
    saved(app, "backup baseline")
    backup = app.backups.create("seed.zip")
    latest = saved(app, "post backup result", at=2.0)
    before = app.backups._counts()
    original = app.recovery.manager.restore
    calls = []

    def fail_once(ref, **kwargs):
        result = original(ref, **kwargs)
        calls.append(ref)
        if len(calls) == 1:
            # The data has actually been replaced. The safety copy must be used
            # after this fault; a failure before touching data would miss it.
            assert app.db.get_task(latest.task_id) is None
            raise RuntimeError("synthetic failure after real replacement")
        return result

    monkeypatch.setattr(app.recovery.manager, "restore", fail_once)
    cli.cmd_host_restore(backup["name"])
    response = json.loads(capsys.readouterr().out)
    assert response.get("ok") is False
    assert response["rollback"]["ok"] is True
    assert response["rollback"]["counts_match_pre_failure"] is True
    assert app.db.get_task(latest.task_id) is not None
    assert app.backups._counts() == before
    assert len(calls) == 2


def test_host_restore_still_restores_terminal_only_instances(app, capsys):
    baseline = saved(app, "baseline")
    backup = app.backups.create("seed.zip")
    latest = saved(app, "new terminal result", at=2.0)
    cli.cmd_host_restore(backup["name"])
    response = json.loads(capsys.readouterr().out)
    assert response["ok"] is True
    assert app.db.get_task(baseline.task_id) is not None
    assert app.db.get_task(latest.task_id) is None


@pytest.mark.parametrize("state", [None, "UNKNOWN_STATUS"])
def test_unknown_stored_states_are_restore_blockers(app, capsys, state):
    backup = app.backups.create("seed.zip")
    active = saved(app, "incomplete legacy state")
    with app.db._lock:
        app.db._conn.execute("UPDATE tasks SET status=? WHERE task_id=?", (state, active.task_id))
        app.db._conn.commit()
    cli.cmd_host_restore(backup["name"])
    response = json.loads(capsys.readouterr().out)
    assert active.task_id in response.get("active_tasks", [])
    assert app.db.get_task(active.task_id) is not None
