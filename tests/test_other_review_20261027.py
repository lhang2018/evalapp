"""Candidate output protocol and benchmark evidence must describe the measured run."""
import copy
import sqlite3

import pytest

from evoagent.assets.repository import AssetRepository
from evoagent.evaluation.host_bench import BenchCase, HostBenchmark, evidence_digest, release_decision
from evoagent.evolution.host import IsolatedCandidateHost


@pytest.fixture
def repository():
    conn = sqlite3.connect(":memory:")
    try:
        yield AssetRepository(conn)
    finally:
        conn.close()


def asset(repository, name, code):
    return repository.register(asset_id=name, asset_type="capability_pack", version="1.0.0",
        namespace="shared_validated", lifecycle_state="candidate",
        content={"entry": "main.py", "files": {"main.py": code}}).ref


def benchmark(repository):
    return HostBenchmark(IsolatedCandidateHost(repository), repeat=1, latency_tolerance=9)


@pytest.fixture
def report(repository):
    base = asset(repository, "review.base", 'print(\'{"ok": false, "value": 0}\')')
    cand = asset(repository, "review.cand", 'print(\'{"ok": true, "value": 1}\')')
    return benchmark(repository).compare(base, cand, [BenchCase("one")])


@pytest.mark.parametrize("ending", ["not-json", "[]", "false", '{"ok": true, "value": NaN}',
                                     '{"ok": true, "value": Infinity}', '{"ok": true, "value": 1e999}'])
def test_last_nonempty_line_must_be_a_strict_json_object(repository, ending):
    base = asset(repository, "base", 'print(\'{"ok": false}\')')
    code = 'print(\'{"ok": true, "value": "early"}\')\nprint(' + repr(ending) + ')\n'
    cand = asset(repository, "candidate", code)
    result = benchmark(repository).compare(base, cand, [BenchCase("one")])
    assert result["cases"][0]["candidate"]["status"] == "invalid_output"
    assert result["release_decision"]["allow"] is False


@pytest.mark.parametrize("field,value", [("value", 2), ("ok", False), ("status", "failed")])
def test_digest_binds_each_case_semantics_and_gate_detects_stale_digest(report, field, value):
    changed = copy.deepcopy(report)
    changed["cases"][0]["candidate"][field] = value
    assert evidence_digest(changed) != report["evidence_digest"]
    assert release_decision(changed)["allow"] is False


def test_digest_binds_timeout_and_policy_without_binding_timing_noise(report):
    changed = copy.deepcopy(report)
    changed["timeout_s"] = 1.0
    assert evidence_digest(changed) != report["evidence_digest"]
    assert release_decision(changed)["allow"] is False
    assert report["policy"] == {"min_coverage": 0.8, "latency_tolerance": 9.0, "max_violations": 0}
    changed = copy.deepcopy(report)
    changed["policy"]["min_coverage"] = 1.0
    assert evidence_digest(changed) != report["evidence_digest"]
    assert release_decision(changed)["allow"] is False
    timing = copy.deepcopy(report)
    timing["cases"][0]["candidate"]["duration_s"] += 1.0
    timing["metrics"]["candidate_latency_median_s"] += 1.0
    assert evidence_digest(timing) == report["evidence_digest"]


@pytest.mark.parametrize("verdict", ["regressed", "inconclusive", "unknown"])
def test_gate_does_not_treat_negative_or_unknown_verdict_as_neutral(report, verdict):
    changed = copy.deepcopy(report)
    changed["verdict"] = verdict
    changed["benefit"]["verdict"] = verdict
    changed["evidence_digest"] = evidence_digest(changed)
    assert release_decision(changed)["allow"] is False


def test_legacy_evidence_requires_rerun_instead_of_automatic_upgrade(report):
    legacy = copy.deepcopy(report)
    legacy["benchmark_version"] = "host-bench/2"
    legacy["evidence_digest"] = evidence_digest(legacy)
    assert release_decision(legacy)["allow"] is False


def test_blank_lines_after_valid_final_json_are_supported(repository):
    base = asset(repository, "base", 'print(\'{"ok": false}\')')
    cand = asset(repository, "candidate", 'print("diagnostic log")\nprint(\'{"ok": true}\')\nprint()')
    result = benchmark(repository).compare(base, cand, [BenchCase("one")])
    assert result["cases"][0]["candidate"]["status"] == "ok"
    assert result["release_decision"]["allow"] is True
