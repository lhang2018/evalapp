"""Real isolated candidate probes; all host files are synthetic temp data."""
import json
import sqlite3

import pytest

from evoagent.assets.repository import AssetRepository
from evoagent.evaluation.host_bench import BenchCase, BenchError, HostBenchmark, benefit_chain
from evoagent.evolution.host import IsolatedCandidateHost
from evoagent.evolution.sandbox import SandboxRunner
from evoagent.integration.candidate_execution import CandidateEvaluationService


@pytest.fixture
def repo():
    conn = sqlite3.connect(":memory:")
    repository = AssetRepository(conn)
    yield repository
    conn.close()


def register(repo, name, code):
    return repo.register(asset_id=name, asset_type="capability_pack", version="1.0.0",
        namespace="shared_validated", lifecycle_state="candidate",
        content={"entry": "main.py", "files": {"main.py": code}}).ref


BASE = 'print(\'{"ok": false}\')\n'
CRASH = '''import json
case = json.load(open("input.json"))
if case["index"] == 4:
    raise RuntimeError("synthetic required-case crash")
print('{"ok": true}')
'''


def runner(repo):
    return HostBenchmark(IsolatedCandidateHost(repo), repeat=1, latency_tolerance=9)


@pytest.mark.parametrize("crashing_side", ["candidate", "baseline"])
def test_required_crash_blocks_even_at_eighty_percent_coverage(repo, crashing_side):
    base = register(repo, "review.base", CRASH if crashing_side == "baseline" else BASE)
    cand = register(repo, "review.cand", CRASH if crashing_side == "candidate" else 'print(\'{"ok": true}\')')
    cases = [BenchCase(f"case-{i}", {"index": i}) for i in range(5)]
    report = runner(repo).compare(base, cand, cases)
    assert report["coverage"]["coverage_ratio"] == 0.8
    assert report["coverage"]["required_coverage_ok"] is False
    assert {"case_id": "case-4", "reason": f"{crashing_side}:crashed"} in report["coverage"]["omitted"]
    assert report["release_decision"]["allow"] is False


@pytest.mark.parametrize("value", ["false", 1, [], None])
def test_non_boolean_ok_is_invalid_output_not_a_valid_measurement(repo, value):
    base = register(repo, "review.base", BASE)
    code = "print(" + repr(json.dumps({"ok": value})) + ")"
    cand = register(repo, "review.cand", code)
    report = runner(repo).compare(base, cand, [BenchCase("one")])
    assert report["cases"][0]["candidate"]["status"] == "invalid_output"
    assert report["release_decision"]["allow"] is False


@pytest.mark.parametrize("cases", [
    [BenchCase("duplicate"), BenchCase("duplicate")],
    [BenchCase("bad-required", required=0)],
    [BenchCase("bad-input", input=[])],
])
def test_direct_benchmark_refuses_ambiguous_case_manifest_before_resolving(repo, cases):
    # Missing assets show that validation precedes any candidate execution.
    with pytest.raises(BenchError):
        runner(repo).compare("missing.base@1.0.0", "missing.cand@1.0.0", cases)


@pytest.mark.parametrize("cases", [
    [{"case_id": "duplicate"}, {"case_id": "duplicate"}],
    [{"case_id": "bad-required", "required": 0}],
    [{"case_id": "bad-domain", "domain": ["general"]}],
])
def test_api_service_does_not_coerce_invalid_case_fields(repo, cases):
    service = CandidateEvaluationService(repo)
    with pytest.raises(ValueError):
        service.evaluate(candidate_id="review", baseline_ref="missing@1.0.0",
            candidate_ref="missing@2.0.0", cases=cases, actor="review", reason="probe")


@pytest.mark.parametrize("operation", ["rename", "replace"])
@pytest.mark.parametrize("direction", ["into_sandbox", "out_of_sandbox"])
def test_sandbox_rejects_file_moves_across_host_boundary(tmp_path, operation, direction):
    host_file = tmp_path / "synthetic-host.txt"
    host_file.write_text("synthetic host data", encoding="utf-8")
    outside = str(host_file)
    if direction == "into_sandbox":
        code = f"import os\nos.{operation}({outside!r}, 'moved.txt')\n"
    else:
        code = f"import os\nopen('inside.txt', 'w').write('inside')\nos.{operation}('inside.txt', {outside!r})\n"
    result = SandboxRunner().run_code(code)
    assert result.status == "violation", result.to_dict()
    assert host_file.read_text(encoding="utf-8") == "synthetic host data"


def test_sandbox_still_allows_file_move_within_owned_directory():
    result = SandboxRunner().run_code(
        "import os\nopen('first.txt', 'w').write('synthetic')\nos.replace('first.txt', 'second.txt')\n"
        "assert open('second.txt').read() == 'synthetic'\n")
    assert result.status == "passed", result.to_dict()


@pytest.mark.parametrize("timeout", [0, float("nan"), float("inf"), 31])
def test_reported_timeout_cannot_differ_from_effective_sandbox_limit(repo, timeout):
    with pytest.raises(BenchError):
        HostBenchmark(IsolatedCandidateHost(repo), timeout=timeout)


@pytest.mark.parametrize("monitor", [{"note": "not a measurement"},
    {"success_rate": float("nan"), "regression": False},
    {"success_rate": 1.2}, {"regression": "false"}])
def test_invalid_or_missing_monitoring_does_not_complete_benefit_chain(monitor):
    report = {"verdict": "improved", "metrics": {"baseline_success_rate": 0.5}}
    chain = benefit_chain(report, monitor)
    assert chain["status"] == "pending"
    assert chain["note"]


def test_explicit_regression_remains_actionable_with_incomplete_monitoring():
    report = {"verdict": "improved", "metrics": {"baseline_success_rate": 0.5}}
    assert benefit_chain(report, {"regression": True})["rollback_required"] is True
    mixed = benefit_chain(report, {"regression": True, "success_rate": float("nan")})
    assert mixed["status"] == "pending" and mixed["rollback_required"] is True


@pytest.mark.parametrize("event,args", [("os.rename", ("one", "two", 123, -1)),
                                       ("os.remove", ("one", 123)),
                                       ("os.chmod", (123, 384, -1))])
def test_audit_hook_refuses_directory_descriptor_redirection(event, args):
    # Synthetic audit events test the actual child hook on Windows as well;
    # this does not claim a POSIX dir_fd filesystem integration run.
    result = SandboxRunner().run_code(f"import sys\nsys.audit({event!r}, *{args!r})\n")
    assert result.status == "violation", result.to_dict()
