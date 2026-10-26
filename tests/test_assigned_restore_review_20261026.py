"""Cold deployment + three real databases + the host-restore CLI entry."""
import json
import sys

from evoagent import cli
from evoagent.app import App
from evoagent.core.task import Task, TaskStatus
from evoagent.deployment.bootstrap import bootstrap, load
from evoagent.memory.store import MemoryItem


def test_deployed_host_cli_restores_three_database_contents_and_identity(tmp_path, monkeypatch, capsys):
    target = tmp_path / "fresh-host"
    manifest = bootstrap(str(target), app_version="assigned-review", source_commit="synthetic-local")
    app = App(data_dir=str(target))
    try:
        baseline = Task(user_goal="baseline terminal fixture", task_status=TaskStatus.SUCCEEDED)
        app.db.save_task(baseline)
        memory = app.memory.write(MemoryItem(content="baseline memory fixture"))
        event = app.bus.emit(event_type="assigned_restore_baseline", component_id="review",
                             payload={"synthetic": True})
        backup = app.backups.create("assigned-three-db.zip")
        expected = app.backups._counts()
        late = Task(user_goal="post backup terminal fixture", task_status=TaskStatus.SUCCEEDED)
        app.db.save_task(late)
        late_memory = app.memory.write(MemoryItem(content="post backup memory fixture"))
        late_event = app.bus.emit(event_type="assigned_restore_late", component_id="review",
                                  payload={"synthetic": True})
        before = app.backups._counts()
        assert all(before[k] > expected[k] for k in ("tasks", "events", "memories"))
        monkeypatch.setattr(cli, "_app", lambda: app)
        monkeypatch.setattr(sys, "argv", ["evoagent", "host-restore", backup["name"]])
        cli.main()
        result = json.loads(capsys.readouterr().out)
        assert result["ok"] is True
        assert result["identity_preserved"] is True and result["counts_matched"] is True
        assert app.backups._counts() == expected
        assert load(str(target))["instance_id"] == manifest["instance_id"]
        assert app.db.get_task(baseline.task_id) is not None
        assert app.db.get_task(late.task_id) is None
        assert app.memory.get(memory.memory_id).content == memory.content
        assert app.memory.get(late_memory.memory_id) is None
        event_ids = {r[0] for r in app.bus._conn.execute("SELECT event_id FROM runtime_events")}
        assert event.event_id in event_ids and late_event.event_id not in event_ids
    finally:
        app.goal_scheduler.stop()
        app.memory.close()
        app.db.close()
