"""Assertions over recorded product observations, not an independent E2E executor.

Nothing here executes commands from evidence. Fixture records never pass product
acceptance. Real-system collectors and independent reviewers remain separate work.
"""
import hashlib
import math
from pathlib import Path
import re

SOURCE = "chat/extracted/157_qa.md"
EXPECTED_IDS = ({f"UX-F{family:02}-{n:02}" for family, count in [(1,2),(2,3),(3,5),(4,4)]
                 for n in range(1,count+1)} | {f"UX-GLOBAL-{n:02}" for n in range(1,4)})
# id -> (observation path, comparison, expected value or another observation path).
ASSERTIONS = {
    "UX-F01-01": [("answer.present","eq",True),("execution.unnecessary_complex_tasks","eq",0)],
    "UX-F01-02": [("context.relevant_retained","eq",True),("context.confirmed_constraints_retained","eq",True)],
    "UX-F02-01": [("artifacts.count","gte",1),("artifacts.accessible","eq",True),
                  ("verification.independent","eq",True),("verification.required_passed","eq",True)],
    "UX-F02-02": [("fault.detected","eq",True),("recovery.bounded","eq",True),
                  ("recovery.attempts","lte_path","recovery.limit"),("recovery.terminated","eq",True)],
    "UX-F02-03": [("verification.required_passed","eq",False),("delivery.fully_completed","eq",False),
                  ("verification.failed_items_preserved","eq",True)],
    "UX-F03-01": [("constraints.new_retained","eq",True),("plan.impact_assessed","eq",True),
                  ("plan.revision_updated","eq",True)],
    "UX-F03-02": [("history.preserved","eq",True),("plan.affected_reevaluated","eq",True),
                  ("results.unaffected_reused","eq",True)],
    "UX-F03-03": [("execution.new_nonessential_actions","eq",0),("inflight.reconciled","eq",True)],
    "UX-F03-04": [("restart.performed","eq",True),("recovery.authoritative_state","eq",True),
                  ("side_effects.blind_replays","eq",0)],
    "UX-F03-05": [("reconciliation.before_resume","eq",True),("side_effects.blind_replays","eq",0)],
    "UX-F04-01": [("sources.only_authorized","eq",True),("facts.traceable","eq",True),
                  ("facts.unknowns_distinguished","eq",True)],
    "UX-F04-02": [("opportunities.high_value_count","eq",0),("suggestions.count","eq",0)],
    "UX-F04-03": [("core.resource_threshold_satisfied","eq",True),("core.resource_samples","gte",1),
                  ("proactive.yielded_under_pressure","eq",True),("proactive.core_state_mutations","eq",0)],
    "UX-F04-04": [("control.proactive_disabled","eq",True),("scheduling.new_nonessential_tasks","eq",0),
                  ("core.execution_available","eq",True),("safety.hints_available","eq",True)],
    "UX-GLOBAL-01": [("run.manifest_after","eq_path","run.manifest_before"),
                     ("update.current_run_hot_swap","eq",False)],
    "UX-GLOBAL-02": [("corrections.used","eq",True),("facts.obsolete_reused","eq",False)],
    "UX-GLOBAL-03": [("budget.exhausted","eq",True),("calls.new_after_exhaustion","eq",0),
                     ("artifacts.available_preserved","eq",True)],
}
MISSING = object()

def source_rows(root):
    text = (root / SOURCE).read_text(encoding="utf-8")
    return {case: (scenario.strip(), condition.strip()) for case, scenario, condition in
            re.findall(r"^\| (UX-(?:F\d\d|GLOBAL)-\d\d) \| ([^|\n]+) \| ([^|\n]+) \|$", text, re.MULTILINE)}

def validate_matrix(matrix, root):
    errors = []
    try:
        if type(matrix["schema_version"]) is not int or matrix["schema_version"] != 1 or matrix["source"]["path"] != SOURCE:
            errors.append("unsupported matrix/source")
        data = (root / SOURCE).read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(data).hexdigest() != matrix["source"]["sha256_lf"]:
            errors.append("source digest changed; re-review the matrix")
        cases = matrix["cases"]
        ids = [case["id"] for case in cases]
        if len(ids) != 17 or set(ids) != EXPECTED_IDS:
            errors.append("matrix requires exactly the original 17 case ids")
        original = source_rows(root)
        if set(original) != EXPECTED_IDS:
            errors.append("source no longer contains exactly the 17 expected cases")
        for case in cases:
            identity = case["id"]
            if case.get("initial_status") != "not_run":
                errors.append("matrix cannot contain a pre-accepted status: " + identity)
            if (case["scenario"], case["source_condition"]) != original.get(identity):
                errors.append("source scenario/condition mismatch: " + identity)
            expected = [list(item) for item in ASSERTIONS.get(identity, [])]
            if case.get("assertions") != expected or not expected:
                errors.append("missing or weakened canonical assertions: " + identity)
            if not isinstance(case.get("steps"), list) or not case["steps"] or not all(isinstance(s,str) and s.strip() for s in case["steps"]):
                errors.append("missing execution steps: " + identity)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        errors.append("invalid matrix: " + str(exc))
    return errors

def value_at(data, path):
    for part in path.split("."):
        if not isinstance(data, dict) or part not in data:
            return MISSING
        data = data[part]
    return data

def compare(observations, predicate):
    path, operator, expected = predicate
    actual = value_at(observations, path)
    if operator.endswith("_path"):
        expected = value_at(observations, expected)
    if actual is MISSING or expected is MISSING:
        return False
    if operator in {"eq", "eq_path"}:
        # bool is an int subclass; 1 must not pass as a measured True.
        if operator == "eq_path" and (not isinstance(actual, str) or not actual or not isinstance(expected, str)):
            return False
        return type(actual) is type(expected) and actual == expected
    numeric = lambda v: type(v) in (int, float) and math.isfinite(v) and v >= 0
    if not numeric(actual) or not numeric(expected):
        return False
    if operator == "gte":
        return actual >= expected
    if operator == "lte_path":
        return expected > 0 and actual <= expected
    return False

def check_artifacts(record, root):
    errors, trace_count = [], 0
    artifacts = record.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return ["missing run evidence artifacts"]
    for item in artifacts:
        try:
            relative = Path(item["path"])
            path = (root / relative).resolve()
            if relative.is_absolute() or not path.is_relative_to(root.resolve()):
                raise ValueError("artifact outside repository")
            if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                raise ValueError("invalid artifact digest")
            if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError("artifact digest mismatch")
            if item.get("kind") in {"execution_log", "event_trace"}:
                trace_count += 1
        except (KeyError, TypeError, ValueError, OSError) as exc:
            errors.append("invalid evidence artifact: " + str(exc))
    if not trace_count:
        errors.append("an execution log or event trace is required")
    return errors

def evaluate(matrix, evidence, root, source_commit):
    """Evaluate provided observations, leaving absent cases explicitly not_run."""
    errors = validate_matrix(matrix, root)
    if not isinstance(source_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        errors.append("an exact 40-character source_commit is required")
    records = {}
    if not isinstance(evidence, dict) or type(evidence.get("schema_version")) is not int or evidence.get("schema_version") != 1 or not isinstance(evidence.get("runs"), list):
        errors.append("evidence must contain schema_version=1 and a runs list")
    else:
        for record in evidence["runs"]:
            if not isinstance(record, dict) or not isinstance(record.get("case_id"), str) or record["case_id"] not in EXPECTED_IDS:
                errors.append("unknown or malformed evidence case")
                continue
            identity = record["case_id"]
            if identity in records:
                errors.append("duplicate evidence case: " + identity)
            records[identity] = record
    results = []
    for identity in sorted(EXPECTED_IDS):
        record = records.get(identity)
        if record is None:
            results.append(dict(id=identity, status="not_run", errors=["no execution evidence"]))
            continue
        problems = []
        if record.get("source_commit") != source_commit:
            problems.append("evidence source commit mismatch")
        environment = record.get("environment")
        if not isinstance(environment, dict) or environment.get("kind") not in {"integration", "device", "production"}:
            problems.append("real execution environment required; fixtures are ineligible")
        elif not all(isinstance(environment.get(k),str) and environment[k].strip() for k in ("run_id","description")):
            problems.append("environment needs a run_id and description")
        problems.extend(check_artifacts(record, root))
        for predicate in ASSERTIONS[identity]:
            if not compare(record.get("observations"), predicate):
                problems.append("assertion failed/missing: " + predicate[0])
        results.append(dict(id=identity,status="failed" if problems or errors else "passed",errors=problems))
    counts = {status:sum(r["status"] == status for r in results) for status in ("passed","failed","not_run")}
    return dict(scope="recorded observations and artifact integrity only; not independent acceptance or publication",
        source_commit=source_commit, matrix_valid=not errors, errors=errors, counts=counts, cases=results,
        all_passed=not errors and counts["passed"]==17)
