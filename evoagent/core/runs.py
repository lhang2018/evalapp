"""Durable execution attempts; logical tasks and external operation keys stay stable."""
import copy
import time
import uuid
from .leases import CURRENT_LEASE, LeaseLost, RunOwnership


class RunManager:
    def __init__(self, db):
        self.db = db
        self.leases = RunOwnership(db)
        self._handles = {}

    def begin(self, task, version_manifest, *, recovery=False):
        expected_run_id, expected_status = task.current_run_id, task.task_status
        previous = task.current_run_id or task.parent_run_id
        if not previous:
            old = self.db.list_runs(task.task_id)
            previous = old[-1]["run_id"] if old else None
        task.parent_run_id = previous
        task.current_run_id = "run_" + uuid.uuid4().hex
        manifest = copy.deepcopy(version_manifest)
        manifest.update(task_id=task.task_id, run_id=task.current_run_id,
                        parent_run_id=previous,
                        runtime_manifest_id="manifest_" + task.current_run_id)
        task.version_manifest = manifest
        task.runtime_manifest["run_id"] = task.current_run_id
        task.runtime_manifest["runtime_manifest_id"] = manifest["runtime_manifest_id"]
        started = time.time()
        token = self.db.claim_run(task, expected_run_id, expected_status, started,
                                  ownership=self.leases, recovery=recovery)
        reset = CURRENT_LEASE.set(token)
        stop, thread = self.leases.start_heartbeat(token)
        self._handles[task.current_run_id] = (token, reset, stop, thread)
        return task.current_run_id, started

    def freeze(self, task):
        manifest = task.version_manifest
        identity = "manifest_" + task.current_run_id
        device = task.runtime_manifest.get("device") or {}
        self.db.save_runtime_manifest(identity, task.task_id,
                                      task.current_run_id,
                                      device.get("device_id"),
                                      device.get("installation_id"),
                                      manifest)

    def finish(self, task, run_id, started):
        # A run without an execution stage (e.g. clarification) still has
        # a final immutable snapshot, honestly retaining unknown references.
        handle = self._handles.pop(run_id, None)
        try:
            self.freeze(task)
            self.db.save_run(run_id, task.task_id, started, time.time(),
                             task.task_status.value, task.version_manifest,
                             task.parent_run_id)
            if task.goal_id and task.is_terminal():
                from ..goals.governance import GoalGovernor
                GoalGovernor(self.db).reconcile(task.goal_id)
        except LeaseLost:
            pass  # A fenced worker must not finalize the new owner's run/task.
        finally:
            if handle:
                token, reset, stop, thread = handle
                stop.set()
                thread.join(0.2)
                try:
                    self.leases.release(token)
                finally:
                    CURRENT_LEASE.reset(reset)
