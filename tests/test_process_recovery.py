"""Actual process death against a shared SQLite file, rather than a paused Task fixture."""
import multiprocessing as mp
import time
from pathlib import Path

import pytest

from evoagent.app import App
from evoagent.core.task import Task, TaskStatus
from evoagent.models.base import Action
from evoagent.models.mock import MockModel
from evoagent.tools.contract import ToolContract, ToolResult
from evoagent.workflows.models import WorkflowDef, WorkflowNode


def _workflow(app, count_file, ready=None):
    def external(_):
        path = Path(count_file)
        path.write_text(str(int(path.read_text()) + 1) if path.exists() else "1")
        if ready:
            ready.send(app.db.list_tasks()[0].task_id)
            time.sleep(60)
        return ToolResult(ok=True, submitted=True, executed=True, contract_valid=True,
                          goal_reached=True, independently_verified=True)
    app.permissions.high_risk_mode = "allow"
    app.tools.register(ToolContract(tool_id="send_once", side_effect_level="external", timeout_policy=90), external)
    wf = WorkflowDef(workflow_id="crash-flow", nodes=[WorkflowNode(node_id="send", tool="send_once")])
    app._workflows[wf.workflow_id] = wf
    return wf


def _crashing_worker(data_dir, mode, ready, count_file):
    app = App(data_dir=data_dir)
    app.controller.runs.leases.ttl = 0.6
    if mode == "model":
        class BlockedModel(MockModel):
            def suggest_action(self, messages, tools):
                ready.send(app.db.list_tasks()[0].task_id)
                time.sleep(60)
        app.controller.router.route = lambda *a, **k: BlockedModel()
        app.submit("现在几点了", intent="time_query")
    else:
        wf = _workflow(app, count_file, ready if mode == "external" else None)
        if mode == "planned":
            transition = app.controller.side_fx.transition
            def stop_after_intent(effect_id, status, **kwargs):
                if status == "submitted":
                    ready.send(app.db.list_tasks()[0].task_id)
                    time.sleep(60)
                return transition(effect_id, status, **kwargs)
            app.controller.side_fx.transition = stop_after_intent
        if mode == "approval":
            app.permissions.high_risk_mode = "confirm"
            task = app.controller.execute(Task(user_goal="send once", workflow_ref=wf.workflow_id), intent_hint="general")
            app.db.decide_task_approval(task.waiting_approvals[0], True, "test")
            authorize = app.approvals.authorize_action
            def stop_after_consume(*args, **kwargs):
                permitted = authorize(*args, **kwargs)
                if permitted:
                    ready.send(kwargs["task_id"])
                    time.sleep(60)
                return permitted
            app.approvals.authorize_action = stop_after_consume
            app.controller.resume_task(task.task_id)
            return
        if mode == "confirmed":
            snapshot = app.controller.workflows._snapshot
            def stop_before_checkpoint(wf, result, done=None, budget=None):
                if done:
                    ready.send(app.db.list_tasks()[0].task_id)
                    time.sleep(60)
                return snapshot(wf, result, done, budget)
            app.controller.workflows._snapshot = stop_before_checkpoint
        if mode == "finalized":
            def stop_before_run_finish(task, rid, started):
                ready.send(task.task_id)
                time.sleep(60)
            app.controller.runs.finish = stop_before_run_finish
        app.controller.execute(Task(user_goal="send once", workflow_ref=wf.workflow_id), intent_hint="general")


def _kill_at_boundary(tmp_path, mode):
    ctx = mp.get_context("spawn")
    receiving, sending = ctx.Pipe(duplex=False)
    count_file = str(tmp_path / "external-count.txt")
    worker = ctx.Process(target=_crashing_worker, args=(str(tmp_path), mode, sending, count_file))
    worker.start()
    try:
        assert receiving.poll(25), "worker never reached fault injection point"
        task_id = receiving.recv()
        # Several TTLs pass while a provider/runner is blocked: heartbeat must renew.
        time.sleep(0.85)
        observer = App(data_dir=str(tmp_path))
        try:
                with pytest.raises(ValueError, match="有效租约|中断准备条件"):
                    observer.controller.recover_task(task_id)
        finally:
            observer.db.close()
        worker.kill()
        worker.join(10)
        assert not worker.is_alive() and worker.exitcode != 0
    finally:
        if worker.is_alive():
            worker.kill(); worker.join(10)
        receiving.close(); sending.close()
    time.sleep(0.8)
    return task_id, count_file


def test_killed_model_process_recovers_parent_run_and_reserved_budget(tmp_path):
    task_id, _ = _kill_at_boundary(tmp_path, "model")
    app = App(data_dir=str(tmp_path))
    try:
        original = app.db.get_task(task_id)
        cp = app.controller.cp_mgr.latest(task_id)
        assert original.task_status == TaskStatus.RUNNING
        assert cp.react_state["model_call_inflight"]
        reserved = cp.budget_state["usage"]["model_calls"]
        assert reserved >= 1
        class Finish(MockModel):
            def suggest_action(self, messages, tools):
                return Action(kind="finish", answer="recovered")
        app.controller.router.route = lambda *a, **k: Finish()
        recovered = app.controller.recover_task(task_id)
        assert recovered.task_status == TaskStatus.SUCCEEDED, recovered.failure_reason
        assert recovered.parent_run_id == original.current_run_id
        assert recovered.current_run_id != original.current_run_id
        assert recovered.runtime_manifest["cost_reconciliation_required"] is True
        assert recovered.result_evidence[-1]["budget"]["usage"]["model_calls"] == reserved + 1
        runs = app.db.list_runs(task_id)
        assert [r["outcome"] for r in runs] == ["INTERRUPTED", "SUCCEEDED"]
        assert runs[0]["finished_at"] and runs[1]["parent_run_id"] == runs[0]["run_id"]
    finally:
        app.db.close()


def test_killed_external_success_without_receipt_is_not_replayed(tmp_path):
    task_id, count_file = _kill_at_boundary(tmp_path, "external")
    app = App(data_dir=str(tmp_path))
    try:
        _workflow(app, count_file)
        interrupted = app.controller.recover_task(task_id)
        assert interrupted.task_status == TaskStatus.SUSPENDED
        assert len(app.db.list_runs(task_id)) == 1  # do not manufacture a child before reconciliation
        assert app.db.list_runs(task_id)[0]["outcome"] == "INTERRUPTED"
        assert {e["status"] for e in app.controller.side_fx.pending(task_id)} == {"unknown"}
        with pytest.raises(PermissionError):
            app.controller.resume_task(task_id)
        app.controller.resume_task(task_id, force=True)
        assert Path(count_file).read_text() == "1"  # force cannot bypass tool gateway
    finally:
        app.db.close()


def test_killed_after_planned_intent_requires_reconciliation_before_any_runner(tmp_path):
    task_id, count_file = _kill_at_boundary(tmp_path, "planned")
    app = App(data_dir=str(tmp_path))
    try:
        assert not Path(count_file).exists()
        _workflow(app, count_file)
        task = app.controller.recover_task(task_id)
        assert task.task_status == TaskStatus.SUSPENDED
        assert {e["status"] for e in app.controller.side_fx.pending(task_id)} == {"unknown"}
        assert len(app.db.list_runs(task_id)) == 1
        assert not Path(count_file).exists()
    finally: app.db.close()


def test_killed_after_approval_consumed_never_reuses_that_authorization(tmp_path):
    task_id, count_file = _kill_at_boundary(tmp_path, "approval")
    app = App(data_dir=str(tmp_path))
    try:
        _workflow(app, count_file)
        app.permissions.high_risk_mode = "confirm"
        old = app.approvals.list_for_task(task_id)
        assert len(old) == 1 and old[0]["status"] == "used"
        assert app.controller.side_fx.history(task_id) == []
        task = app.controller.recover_task(task_id)
        assert task.task_status == TaskStatus.SUSPENDED
        assert len(task.waiting_approvals) == 1 and task.waiting_approvals[0] != old[0]["approval_id"]
        assert app.approvals.get(old[0]["approval_id"])["status"] == "used"
        assert not Path(count_file).exists()
    finally: app.db.close()


def test_confirmed_effect_before_checkpoint_reuses_receipt_and_budget(tmp_path):
    task_id, count_file = _kill_at_boundary(tmp_path, "confirmed")
    app = App(data_dir=str(tmp_path))
    try:
        _workflow(app, count_file)
        assert {e["status"] for e in app.controller.side_fx.history(task_id)} == {"confirmed"}
        assert app.controller.cp_mgr.latest(task_id).completed_nodes == []
        task = app.controller.recover_task(task_id)
        assert task.task_status == TaskStatus.SUCCEEDED, task.failure_reason
        assert Path(count_file).read_text() == "1"
        assert task.result_evidence[-1]["budget"]["usage"]["tool_calls"] == 1
        assert task.runtime_manifest["workflow_budget"]["usage"]["tool_calls"] == 1
    finally: app.db.close()


def test_killed_after_final_checkpoint_only_closes_run(tmp_path):
    task_id, count_file = _kill_at_boundary(tmp_path, "finalized")
    app = App(data_dir=str(tmp_path))
    try:
        task = app.controller.recover_task(task_id)
        assert task.task_status == TaskStatus.SUCCEEDED
        assert len(app.db.list_runs(task_id)) == 1
        assert app.db.list_runs(task_id)[0]["outcome"] == "SUCCEEDED"
        assert Path(count_file).read_text() == "1"
    finally: app.db.close()


def _compete(data_dir, task_id, gate, answer):
    from evoagent.core.persistence import Database
    from evoagent.core.runs import RunManager
    db = Database(str(Path(data_dir) / "compete.db"))
    task = db.get_task(task_id)
    gate.wait(15)
    manager = RunManager(db)
    try:
        rid, started = manager.begin(task, {}, recovery=True)
        answer.put("claimed")
        manager.finish(task, rid, started)
    except ValueError:
        answer.put("rejected")
    finally:
        db.close()


def test_two_processes_compete_for_one_expired_generation(tmp_path):
    from evoagent.core.persistence import Database
    from evoagent.core.runs import RunManager
    from evoagent.core.leases import CURRENT_LEASE
    db = Database(str(tmp_path / "compete.db"))
    manager = RunManager(db)
    task = Task()
    rid, _ = manager.begin(task, {})
    task.task_status = TaskStatus.RUNNING; db.save_task(task)
    db.save_checkpoint("cp", task.task_id, time.time(), {"budget_state": {"usage": {"model_calls": 1}}})
    token, reset, stop, thread = manager._handles.pop(rid)
    stop.set(); thread.join(2); CURRENT_LEASE.reset(reset)
    db._conn.execute("UPDATE run_leases SET expires_at=0"); db._conn.commit(); db.close()
    ctx = mp.get_context("spawn"); gate = ctx.Event(); results = ctx.Queue()
    workers = [ctx.Process(target=_compete, args=(str(tmp_path), task.task_id, gate, results)) for _ in range(2)]
    try:
        for worker in workers: worker.start()
        gate.set()
        assert sorted([results.get(timeout=25), results.get(timeout=25)]) == ["claimed", "rejected"]
        for worker in workers:
            worker.join(10); assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive(): worker.kill(); worker.join(10)
        results.close()
