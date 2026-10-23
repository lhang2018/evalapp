"""Persisted evidence-backed proposals and deterministic expected-utility ranking."""
import json

from ..core.budget import task_budget_usage
from .governance import GoalGovernor, digest
from .store import GoalStore, _json, _number, _text

RISK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class OpportunityService:
    def __init__(self, db, *, clock=None):
        self.db = db
        self.goals = GoalStore(db, clock=clock)
        self.clock = self.goals.clock

    def _get(self, candidate_id):
        row = self.db._conn.execute("SELECT data FROM goal_opportunities WHERE candidate_id=?", (candidate_id,)).fetchone()
        if row is None:
            raise KeyError("opportunity does not exist")
        return json.loads(row[0])

    def _source(self, kind, reference):
        if kind == "goal":
            goal = self.goals._get(reference)
            return goal["title"], goal["success_criteria"], True
        if kind != "task":
            raise ValueError("source must be a registered goal or terminal task")
        task = self.db.get_task(reference)
        if task is None or not task.is_terminal():
            raise ValueError("source task must exist and be terminal")
        usage = task_budget_usage(task, self.db)
        pending = self.db._conn.execute("SELECT 1 FROM task_side_effects WHERE task_id=? "
                                       "AND status IN ('planned','submitted','unknown')", (reference,)).fetchone()
        known = not (pending or usage.get("unknown_model_costs") or usage.get("unknown_tool_costs") or
                     task.runtime_manifest.get("cost_reconciliation_required"))
        title = ("Review failure: " if task.task_status.value != "SUCCEEDED" else "Improve outcome: ") + task.user_goal
        return title, ["independently verify the proposed improvement"], bool(known)

    def discover(self, source_kind, source_id, *, actor, reason, expires_at):
        actor, reason = _text(actor, "actor"), _text(reason, "reason")
        _text(source_id, "source_id")
        _number(expires_at, "expires_at", positive=True)
        source_ref = source_kind + ":" + source_id
        with self.goals._write():
            title, criteria, known = self._source(source_kind, source_id)
            row = self.db._conn.execute("SELECT data FROM goal_opportunities WHERE source_ref=?", (source_ref,)).fetchone()
            if row:
                return json.loads(row[0])
            if expires_at <= self.clock():
                raise ValueError("opportunity is expired")
            result = dict(candidate_id="opp_" + digest(source_ref)[:32], source_ref=source_ref,
                source_kind=source_kind, source_id=source_id, title=title, success_criteria=criteria,
                created_at=self.clock(), expires_at=expires_at, source_known=known, assessment=None,
                registered_goal_id=None, revision=1, actor=actor, reason=reason)
            self.db._conn.execute("INSERT INTO goal_opportunities VALUES (?,?,?)",
                (result["candidate_id"], source_ref, _json(result)))
            return result

    def assess(self, candidate_id, *, expected_benefit, evidence_confidence, estimated_cost,
               opportunity_cost, risk, actor, reason, expected_revision):
        actor, reason = _text(actor, "actor"), _text(reason, "reason")
        for key, value in (("expected_benefit", expected_benefit), ("evidence_confidence", evidence_confidence),
                           ("opportunity_cost", opportunity_cost)):
            _number(value, key)
            if value > 1_000_000_000:
                raise ValueError(key + " exceeds supported utility range")
        if evidence_confidence > 1 or risk not in RISK:
            raise ValueError("invalid confidence or risk")
        if estimated_cost is not None:
            _number(estimated_cost, "estimated_cost")
            if estimated_cost > 1_000_000_000:
                raise ValueError("estimated_cost exceeds supported range")
        with self.goals._write():
            result = self._get(candidate_id)
            self.goals._version(result, expected_revision)
            result.update(assessment=dict(expected_benefit=expected_benefit, evidence_confidence=evidence_confidence,
                estimated_cost=estimated_cost, opportunity_cost=opportunity_cost, risk=risk,
                actor=actor, reason=reason), revision=result["revision"] + 1)
            self.db._conn.execute("UPDATE goal_opportunities SET data=? WHERE candidate_id=?", (_json(result), candidate_id))
            return result

    def ranked(self, *, policy_ref="manual-review/v1"):
        with self.db._lock:
            policy = GoalGovernor(self.db, clock=self.clock).policy(policy_ref)
            results = []
            for row in self.db._conn.execute("SELECT data FROM goal_opportunities"):
                candidate = json.loads(row[0])
                assessment = candidate["assessment"]
                try:
                    _, _, source_known = self._source(candidate["source_kind"], candidate["source_id"])
                except (KeyError, ValueError):
                    source_known = False
                candidate.update(score=None, ranking_policy_ref=policy_ref, ranking_state="needs_evidence")
                if candidate["expires_at"] <= self.clock():
                    candidate["ranking_state"] = "expired"
                elif assessment and source_known and assessment["estimated_cost"] is not None and assessment["evidence_confidence"] > 0:
                    freshness = min(1, (candidate["expires_at"] - self.clock()) /
                                    max(.001, candidate["expires_at"] - candidate["created_at"]))
                    candidate["score"] = (assessment["expected_benefit"] * assessment["evidence_confidence"] * freshness
                        - policy.cost_weight * assessment["estimated_cost"] - policy.risk_weight * RISK[assessment["risk"]]
                        - assessment["opportunity_cost"])
                    candidate["ranking_state"] = "ranked"
                results.append(candidate)
            return sorted(results, key=lambda c: (c["score"] is None, -(c["score"] or 0), c["candidate_id"]))

    def register(self, candidate_id, *, actor):
        actor = _text(actor, "actor")
        with self.db._lock:
            candidate = self._get(candidate_id)
        if candidate["registered_goal_id"]:
            return self.goals.get(candidate["registered_goal_id"])
        if candidate["expires_at"] <= self.clock():
            raise PermissionError("expired opportunity")
        goal = self.goals.create(candidate["title"], actor=actor, goal_type="opportunity",
            success_criteria=candidate["success_criteria"], deadline=candidate["expires_at"],
            command_id="opportunity-" + digest(candidate_id)[:40])
        with self.goals._write():
            current = self._get(candidate_id)
            if not current["registered_goal_id"]:
                current["registered_goal_id"] = goal["goal_id"]
                current["registered_assessment"] = candidate["assessment"]
            self.db._conn.execute("UPDATE goal_opportunities SET data=? WHERE candidate_id=?", (_json(current), candidate_id))
        return goal

    def outcome(self, candidate_id):
        with self.db._lock:
            candidate = self._get(candidate_id)
            result = dict(candidate_id=candidate_id, source_ref=candidate["source_ref"],
                expected=candidate.get("registered_assessment", candidate["assessment"]), goal_id=candidate["registered_goal_id"],
                observed=None, production_benefit_verified=False)
            if not candidate["registered_goal_id"]:
                return result
            governor = GoalGovernor(self.db, clock=self.clock)
            report = governor.report(candidate["registered_goal_id"])
            task_ids = {task.task_id for task, _ in governor._tasks(candidate["registered_goal_id"])}
            calls = [json.loads(row[0]) for row in self.db._conn.execute("SELECT data FROM budget_reservations")]
            calls = [call for call in calls if call.get("context", {}).get("task_id") in task_ids]
            receipts_complete = True
            for task, _ in governor._tasks(candidate["registered_goal_id"]):
                usage = task_budget_usage(task, self.db)
                expected_calls = usage.get("model_calls", 0) + usage.get("tool_calls", 0)
                actual_calls = sum(call["actual"].get("model_calls", 0) + call["actual"].get("tool_calls", 0)
                    for call in calls if call.get("context", {}).get("task_id") == task.task_id and call.get("actual") is not None)
                if actual_calls < expected_calls:
                    receipts_complete = False
            result["observed"] = dict(task_refs=sorted(task_ids), fulfilled_objectives=report["fulfilled"],
                settled_cost_usd=sum(call["actual"]["cost_micros"] for call in calls if call["state"] == "settled")/1_000_000,
                cost_complete=receipts_complete and not report["pending"] and not any(call["state"] in {"unknown", "reserved"} for call in calls) and
                    all(item["known"] for item in report["observations"]),
                pending_tasks=report["pending"], stop_reason=report["reason"])
            return result
