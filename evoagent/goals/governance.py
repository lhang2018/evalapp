"""Bounded autonomy rules over the existing Goal/Task authority (ALN-14)."""
from dataclasses import asdict, dataclass
import hashlib
import json
import re
import unicodedata

from ..core.budget import task_budget_usage
from ..core.task import Task
from .store import GoalConflict, GoalStore, _json, _number, _text


@dataclass(frozen=True)
class GovernancePolicy:
    max_attempts: int = 8
    max_no_progress: int = 3
    max_failures: int = 2
    max_repeated_states: int = 3
    idle_seconds: float = 900
    max_depth: int = 4
    max_children: int = 8
    max_descendants: int = 32
    cost_weight: float = 1
    risk_weight: float = 1

    @classmethod
    def parse(cls, values):
        if not isinstance(values, dict) or set(values) - set(cls.__dataclass_fields__):
            raise ValueError("unknown governance policy field")
        result = cls(**values)
        for key, value in asdict(result).items():
            _number(value, key, positive=True)
            if key.startswith("max_") and (type(value) is not int or value > 10000):
                raise ValueError(key + " must be an integer in 1..10000")
            if not key.startswith("max_") and value > 1_000_000_000:
                raise ValueError(key + " exceeds supported policy range")
        if result.max_depth > 16 or result.max_descendants > 1000:
            raise ValueError("governance tree is too large")
        return result


MANUAL = GovernancePolicy(max_attempts=1, max_no_progress=1)
OBJECTIVE_TYPES = {"result_contains", "result_matches", "data_equals", "tool_goal_reached"}


def digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def purpose(title):
    return " ".join(unicodedata.normalize("NFKC", title).casefold().split())


class GoalGovernor:
    def __init__(self, db, *, controller=None, clock=None):
        self.db, self.controller = db, controller
        self.goals = GoalStore(db, clock=clock)
        self.clock = self.goals.clock

    def policy(self, ref):
        if ref == "manual-review/v1":
            return MANUAL
        row = self.db._conn.execute("SELECT data FROM goal_policies WHERE ref=?", (ref,)).fetchone()
        if row is None:
            raise PermissionError("unregistered no-progress policy")
        return GovernancePolicy.parse(json.loads(row[0])["rules"])

    def install_policy(self, ref, rules, *, actor, reason):
        ref, actor, reason = _text(ref, "policy ref"), _text(actor, "actor"), _text(reason, "reason")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+/v[1-9][0-9]*", ref) or ref == "manual-review/v1":
            raise ValueError("use a versioned custom policy ref")
        parsed = asdict(GovernancePolicy.parse(rules))
        with self.goals._write():
            old = self.db._conn.execute("SELECT data FROM goal_policies WHERE ref=?", (ref,)).fetchone()
            if old:
                result = json.loads(old[0])
                if result["rules"] != parsed:
                    raise GoalConflict("policy version is immutable")
                return result
            result = dict(ref=ref, rules=parsed, actor=actor, reason=reason, created_at=self.clock())
            self.db._conn.execute("INSERT INTO goal_policies VALUES (?,?)", (ref, _json(result)))
            return result

    def _control(self, goal):
        row = self.db._conn.execute("SELECT data FROM goal_controls WHERE goal_id=?", (goal["goal_id"],)).fetchone()
        return json.loads(row[0]) if row else dict(objectives=[], excluded_tasks=[], seen_checks=[],
                                                epoch_started_at=goal["created_at"])

    def configure(self, goal_id, objectives, *, actor, reason, expected_revision):
        actor, reason = _text(actor, "actor"), _text(reason, "reason")
        if not isinstance(objectives, list) or not 1 <= len(objectives) <= 32:
            raise ValueError("provide 1..32 objective criteria")
        for spec in objectives:
            if not isinstance(spec, dict) or spec.get("type") not in OBJECTIVE_TYPES:
                raise ValueError("objective requires a supported independent rule")
            allowed = {"type", "value"} if spec["type"] in {"result_contains", "data_equals"} else (
                {"type", "pattern"} if spec["type"] == "result_matches" else {"type"})
            if set(spec) != allowed:
                raise ValueError("invalid objective fields")
            if "value" in spec and (not isinstance(spec["value"], str) or not spec["value"].strip()):
                raise ValueError("objective value must be a nonempty string")
            if "pattern" in spec:
                _text(spec["pattern"], "pattern")
                try:
                    re.compile(spec["pattern"])
                except re.error as exc:
                    raise ValueError("invalid objective pattern") from exc
        with self.goals._write():
            goal = self.goals._get(goal_id)
            self.goals._version(goal, expected_revision)
            self.goals._grants(goal_id, "advance", active=False)
            self.policy(goal["no_progress_policy_ref"])
            if self._tasks(goal_id):
                raise GoalConflict("objectives must be fixed before the first task")
            control = dict(objectives=objectives, excluded_tasks=[], seen_checks=[], epoch_started_at=self.clock(),
                           actor=actor, reason=reason)
            self.db._conn.execute("INSERT INTO goal_controls VALUES (?,?) ON CONFLICT(goal_id) DO UPDATE SET data=excluded.data",
                                  (goal_id, _json(control)))
            goal["revision"] += 1
            return self.goals._save(goal, actor, reason, "goal_objectives_configured")

    def _tasks(self, goal_id):
        rows = self.db._conn.execute("WITH RECURSIVE family(id) AS (SELECT ? UNION "
            "SELECT g.goal_id FROM goals g JOIN family f ON g.parent_goal_id=f.id) "
            "SELECT t.data,b.action FROM tasks t JOIN goal_tasks b ON b.task_id=t.task_id "
            "WHERE t.goal_id IN (SELECT id FROM family)", (goal_id,)).fetchall()
        return sorted([(Task.from_dict(json.loads(row[0])), row[1]) for row in rows],
                      key=lambda pair: (pair[0].updated_at, pair[0].task_id))

    def enabled(self, goal, action=None):
        if goal["no_progress_policy_ref"] not in {None, "manual-review/v1"}:
            return True
        return action == "advance" or any(action == "advance" for _, action in self._tasks(goal["goal_id"]))

    def _observation(self, task):
        row = self.db._conn.execute("SELECT data FROM goal_observations WHERE task_id=?", (task.task_id,)).fetchone()
        if row:
            return json.loads(row[0])
        usage = task_budget_usage(task, self.db)
        known = not (usage.get("unknown_model_costs") or usage.get("unknown_tool_costs") or
                     task.runtime_manifest.get("cost_reconciliation_required"))
        pending = self.db._conn.execute("SELECT 1 FROM task_side_effects WHERE task_id=? AND status IN ('planned','submitted','unknown')",
                                        (task.task_id,)).fetchone()
        known = known and not bool(pending)
        checks = []
        for evidence in task.result_evidence or []:
            if not isinstance(evidence, dict) or evidence.get("uncertain"):
                continue
            for check in evidence.get("checks", []):
                if isinstance(check, dict) and check.get("passed") is True and not check.get("uncertain"):
                    spec = check.get("spec")
                    if isinstance(spec, dict) and spec.get("type") in OBJECTIVE_TYPES:
                        checks.append(digest(spec))
        success = task.task_status.value == "SUCCEEDED"
        return dict(task_id=task.task_id, goal_id=task.goal_id, run_id=task.current_run_id,
                    finished_at=task.updated_at, success=success, known=bool(known),
                    checks=sorted(set(checks)) if known else [],
                    state=digest([task.task_status.value, task.result, task.failure_reason, sorted(set(checks))]),
                    failure_reason=task.failure_reason or "", usage=usage)

    def report(self, goal_id, *, new_attempt=False):
        with self.db._lock:
            goal = self.goals._get(goal_id)
            policy = self.policy(goal["no_progress_policy_ref"] or "manual-review/v1")
            control = self._control(goal)
            all_tasks = self._tasks(goal_id)
            tasks = [(task, action) for task, action in all_tasks if task.task_id not in control["excluded_tasks"]]
            seen = set(control["seen_checks"])
            objectives = {digest(spec) for spec in control["objectives"]}
            failures = stalled = 0
            states = {}
            last_progress = control["epoch_started_at"]
            observations = []
            for task, _ in tasks:
                if not task.is_terminal():
                    continue
                observation = self._observation(task)
                observations.append(observation)
                fresh = set(observation["checks"]) & objectives - seen
                if fresh:
                    seen.update(fresh)
                    stalled = 0
                    last_progress = max(last_progress, observation["finished_at"])
                else:
                    stalled += 1
                failures = 0 if observation["success"] and observation["known"] else failures + 1
                states[observation["state"]] = states.get(observation["state"], 0) + 1
            reason = None
            if any(not item["known"] for item in observations):
                reason = "unknown_evidence"
            elif any("预算耗尽" in item["failure_reason"] or "budget:" in item["failure_reason"] for item in observations):
                reason = "budget_exhausted"
            elif objectives and objectives <= seen:
                reason = "objectives_satisfied"
            elif failures >= policy.max_failures:
                reason = "repeated_failure"
            elif max(states.values(), default=0) >= policy.max_repeated_states:
                reason = "repeated_state"
            elif stalled >= policy.max_no_progress:
                reason = "no_improvement"
            elif self.clock() - last_progress >= policy.idle_seconds:
                reason = "idle_timeout"
            elif len(tasks) >= policy.max_attempts and (new_attempt or all(t.is_terminal() for t, _ in tasks)):
                reason = "attempt_limit"
            return dict(goal_id=goal_id, policy_ref=goal["no_progress_policy_ref"] or "manual-review/v1",
                        reason=reason, attempts=len(tasks), no_progress=stalled, failures=failures,
                        fulfilled=sorted(seen), last_progress_at=last_progress,
                        task_refs=[item["task_id"] for item in observations], observations=observations,
                        pending=[task.task_id for task, _ in tasks if not task.is_terminal()])

    def guard(self, lineage, action, *, new_attempt=False):
        for goal in lineage:
            if not self.enabled(goal, action):
                continue
            report = self.report(goal["goal_id"], new_attempt=new_attempt)
            if report["reason"]:
                raise PermissionError("Goal stopped: " + report["reason"])
            if new_attempt and action == "advance" and report["pending"]:
                raise GoalConflict("Goal family already has an unfinished task; resume it")

    def guard_child(self, parent_id, title):
        lineage = self.goals._lineage(parent_id)
        if any(self.enabled(goal) for goal in lineage):
            self.goals._grants(parent_id, "suggest")
        self.guard(lineage, "suggest", new_attempt=True)
        for depth, ancestor in enumerate(lineage, start=1):
            if not self.enabled(ancestor):
                continue
            if ancestor["status"] not in {"ready", "running"}:
                raise PermissionError("parent Goal is not active")
            policy = self.policy(ancestor["no_progress_policy_ref"])
            if depth > policy.max_depth:
                raise GoalConflict("derived Goal depth limit")
            descendants = self.db._conn.execute("WITH RECURSIVE family(id) AS (SELECT goal_id FROM goals WHERE parent_goal_id=? "
                "UNION SELECT g.goal_id FROM goals g JOIN family f ON g.parent_goal_id=f.id) SELECT id FROM family",
                (ancestor["goal_id"],)).fetchall()
            if len(descendants) >= policy.max_descendants:
                raise GoalConflict("derived Goal total count limit")
            count = self.db._conn.execute("SELECT COUNT(*) FROM goals WHERE parent_goal_id=?", (parent_id,)).fetchone()[0]
            if count >= policy.max_children:
                raise GoalConflict("derived Goal child count limit")
            normalized = purpose(title)
            relatives = [self.goals._get(row[0]) for row in descendants] + lineage
            if any(purpose(relative["title"]) == normalized for relative in relatives):
                raise GoalConflict("duplicate or cyclic Goal purpose")

    def reconcile(self, goal_id):
        with self.goals._write():
            lineage = self.goals._lineage(goal_id)
            reports = []
            for goal in lineage:
                if not self.enabled(goal):
                    continue
                try:
                    report = self.report(goal["goal_id"])
                except PermissionError:
                    report = dict(goal_id=goal["goal_id"], policy_ref=goal["no_progress_policy_ref"],
                                  reason="unregistered_policy", observations=[])
                for observation in report["observations"]:
                    self.db._conn.execute("INSERT OR IGNORE INTO goal_observations VALUES (?,?,?)",
                        (observation["task_id"], observation["goal_id"], _json(observation)))
                if report["reason"] and goal["status"] in {"ready", "running", "waiting", "review"}:
                    target = "review" if report["reason"] == "objectives_satisfied" else "paused"
                    if target != goal["status"]:
                        goal.update(status=target, revision=goal["revision"] + 1,
                                    governance_stop={k: v for k, v in report.items() if k != "observations"})
                        self.goals._save(goal, "runtime:governor", report["reason"], "goal_governance_stopped")
                reports.append(report)
            return reports

    def review(self, goal_id, *, actor, reason, expected_revision, evidence_refs):
        actor, reason = _text(actor, "actor"), _text(reason, "reason")
        if not isinstance(evidence_refs, list) or not evidence_refs or any(not isinstance(v, str) or not v.strip() for v in evidence_refs):
            raise ValueError("review requires evidence references")
        with self.goals._write():
            goal = self.goals._get(goal_id)
            self.goals._version(goal, expected_revision)
            if goal["status"] != "paused":
                raise GoalConflict("review requires a paused Goal")
            self.goals._grants(goal_id, "advance", active=False)
            report = self.report(goal_id)
            if report["pending"]:
                raise GoalConflict("resolve pending work and charges before resuming")
            if report["reason"] == "unknown_evidence":
                for observation in report["observations"]:
                    if observation["known"]:
                        continue
                    task_id = observation["task_id"]
                    pending = self.db._conn.execute("SELECT 1 FROM task_side_effects WHERE task_id=? "
                        "AND status IN ('planned','submitted','unknown')", (task_id,)).fetchone()
                    records = [json.loads(row[0]) for row in self.db._conn.execute("SELECT data FROM budget_reservations")]
                    records = [row for row in records if row.get("context", {}).get("task_id") == task_id]
                    if pending or not records or any(row["state"] not in {"settled", "refunded"} for row in records):
                        raise GoalConflict("resolve pending work and charges before resuming")
                    unknowns = (observation["usage"].get("unknown_model_costs", 0) +
                                observation["usage"].get("unknown_tool_costs", 0))
                    if sum(row.get("resolution") == "reconcile" for row in records) < unknowns:
                        raise GoalConflict("unknown charges need a durable reconciliation receipt")
            control = self._control(goal)
            control.update(excluded_tasks=[task.task_id for task, _ in self._tasks(goal_id)],
                           seen_checks=report["fulfilled"], epoch_started_at=self.clock())
            self.db._conn.execute("INSERT INTO goal_controls VALUES (?,?) ON CONFLICT(goal_id) DO UPDATE SET data=excluded.data",
                                  (goal_id, _json(control)))
            goal.update(status="ready", revision=goal["revision"] + 1)
            goal["evidence_refs"] = sorted(set(goal["evidence_refs"] + evidence_refs))
            return self.goals._save(goal, actor, reason, "goal_governance_reviewed")

    def advance(self, goal_id, *, command_id, max_steps=1):
        if self.controller is None:
            raise RuntimeError("advancement requires the real runtime controller")
        if type(max_steps) is not int or not 1 <= max_steps <= 32:
            raise ValueError("max_steps must be 1..32")
        _text(command_id, "command_id")
        if len(command_id) > 128:
            raise ValueError("command_id is too long")
        with self.goals._write():
            self.goals._get(goal_id)
            row = self.db._conn.execute("SELECT data FROM goal_advances WHERE goal_id=? AND command_id=?",
                                        (goal_id, command_id)).fetchone()
            if row:
                request = json.loads(row[0])
                if request["max_steps"] != max_steps:
                    raise GoalConflict("advancement command changed max_steps")
                if request["result"] is not None:
                    return request["result"]
                # A crash or concurrent request is not permission to dispatch
                # again. Recover references only; resume the existing Task via
                # its normal recovery gateway, or issue a new explicit command.
                tasks = []
                for step in range(max_steps):
                    key = "advance-" + digest([goal_id, command_id, step])[:40]
                    linked = self.db._conn.execute("SELECT result FROM goal_commands WHERE scope=? AND command_id=?",
                        ("goal:task:" + goal_id, key)).fetchone()
                    if linked:
                        task = self.db.get_task(json.loads(linked[0])["task_id"])
                        if task:
                            tasks.append(task.to_dict())
                return dict(goal=self.goals._get(goal_id), tasks=tasks, pending_request=True)
            request = dict(max_steps=max_steps, result=None)
            self.db._conn.execute("INSERT INTO goal_advances VALUES (?,?,?)", (goal_id, command_id, _json(request)))
        result = self._advance(goal_id, command_id, max_steps)
        with self.goals._write():
            request["result"] = result
            self.db._conn.execute("UPDATE goal_advances SET data=? WHERE goal_id=? AND command_id=?",
                                  (_json(request), goal_id, command_id))
        return result

    def _advance(self, goal_id, command_id, max_steps):
        results = []
        for step in range(max_steps):
            self.reconcile(goal_id)
            goal = self.goals.get(goal_id)
            if goal["status"] not in {"ready", "running"}:
                break
            control = self._control(goal)
            task = self.controller.submit_task(goal["title"], goal_id=goal_id, origin="derived",
                goal_action="advance", goal_command_id="advance-" + digest([goal_id, command_id, step])[:40],
                acceptance=control["objectives"], wait=True)
            results.append(task.to_dict())
            self.reconcile(goal_id)
            if not task.is_terminal():
                break
        return dict(goal=self.goals.get(goal_id), tasks=results)
