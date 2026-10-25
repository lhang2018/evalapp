"""ALN-10-E：真实宿主上的候选对照基准与收益/依赖回归证据测试。

全部资产都注册在真实资产库（SQLite）并按精确版本+摘要消费，执行走
ALN-09-H 的隔离候选宿主（一次性沙箱、超时强杀、网络/子进程阻断），
不使用进程内假函数。
"""

import json
import sqlite3

import pytest

from evoagent.assets.repository import AssetRepository
from evoagent.evaluation.host_bench import (BenchCase, BenchError, HostBenchmark,
                                            benefit_chain, evidence_digest)
from evoagent.evolution.host import CandidateAssetError, IsolatedCandidateHost

BASELINE_CODE = """\
import json
case = json.load(open("input.json", encoding="utf-8"))
value = int(case.get("value", 0))
print(json.dumps({"case_id": case.get("case_id"), "ok": value % 2 == 0, "value": value}))
"""

IMPROVED_CODE = """\
import json
case = json.load(open("input.json", encoding="utf-8"))
value = int(case.get("value", 0))
print(json.dumps({"case_id": case.get("case_id"), "ok": True, "value": value}))
"""

REGRESSED_CODE = """\
import json
case = json.load(open("input.json", encoding="utf-8"))
value = int(case.get("value", 0))
print(json.dumps({"case_id": case.get("case_id"), "ok": value % 3 == 0, "value": value}))
"""

RANDOM_CODE = """\
import json, os
case = json.load(open("input.json", encoding="utf-8"))
print(json.dumps({"case_id": case.get("case_id"), "ok": True, "value": os.urandom(4).hex()}))
"""

SLOW_CODE = """\
import time
time.sleep(5)
print('{"ok": true}')
"""

NETWORK_CODE = """\
import socket
socket.getaddrinfo("localhost", 80)
print('{"ok": true}')
"""


@pytest.fixture
def repository():
    repo = AssetRepository(sqlite3.connect(":memory:"))
    yield repo


def register(repo, code, *, version="1.0.0", asset_id="pack.bench"):
    return repo.register(asset_id=asset_id, asset_type="capability_pack",
                         namespace="shared_validated", version=version,
                         content={"entry": "main.py",
                                  "files": {"main.py": code}},
                         lifecycle_state="candidate")


def cases(required=True):
    return [BenchCase("c%d" % value, {"case_id": "c%d" % value, "value": value},
                      domain="general", required=required)
            for value in range(1, 6)]


def bench(repository, *, timeout=2.0, repeat=2, latency_tolerance=5.0):
    return HostBenchmark(IsolatedCandidateHost(repository), timeout=timeout,
                         repeat=repeat, latency_tolerance=latency_tolerance,
                         source="tests/host-bench")


def test_improved_candidate_produces_reproducible_benefit_and_allows_release(repository):
    baseline = register(repository, BASELINE_CODE, version="1.0.0")
    candidate = register(repository, IMPROVED_CODE, version="1.1.0")
    report = bench(repository).compare(baseline.ref, candidate.ref, cases())
    assert report["reproducible"] is True
    assert report["verdict"] == "improved", report["benefit"]
    assert report["metrics"]["baseline_success_rate"] == 0.4
    assert report["metrics"]["candidate_success_rate"] == 1.0
    assert report["metrics"]["success_delta"] == pytest.approx(0.6)
    assert report["regression_cases"] == []
    assert report["coverage"]["coverage_ratio"] == 1.0
    assert report["coverage"]["required_coverage_ok"] is True
    assert report["release_decision"]["allow"] is True
    # 身份绑定：精确版本 + 摘要
    assert report["baseline"]["version"] == "1.0.0"
    assert report["candidate"]["version"] == "1.1.0"
    assert report["baseline"]["content_digest"] == baseline.content_digest
    assert report["candidate"]["content_digest"] == candidate.content_digest
    # 可复现：同一输入/同一资产重复对照得到同一证据摘要
    again = bench(repository).compare(baseline.ref, candidate.ref, cases())
    assert evidence_digest(again) == report["evidence_digest"]
    # 发布前收益链在缺少发布后监测时明确 pending，不冒充已验证收益
    chain = benefit_chain(report)
    assert chain["status"] == "pending" and chain["rollback_required"] is False
    assert "未闭合" in chain["note"]


def test_regressed_candidate_blocks_release_and_lists_regression_cases(repository):
    baseline = register(repository, BASELINE_CODE, version="1.0.0")
    candidate = register(repository, REGRESSED_CODE, version="1.2.0")
    report = bench(repository).compare(baseline.ref, candidate.ref, cases())
    assert report["regression_cases"] == ["c2", "c4"]
    assert report["verdict"] == "regressed"
    decision = report["release_decision"]
    assert decision["allow"] is False
    assert any("回归" in item for item in decision["blockers"])


def test_non_deterministic_candidate_is_inconclusive_and_blocked(repository):
    baseline = register(repository, BASELINE_CODE, version="1.0.0")
    candidate = register(repository, RANDOM_CODE, version="1.3.0")
    report = bench(repository).compare(baseline.ref, candidate.ref, cases())
    assert report["reproducible"] is False
    assert report["verdict"] == "inconclusive"
    assert report["release_decision"]["allow"] is False
    assert "对照不可复现" in report["release_decision"]["blockers"]


def test_timeout_is_recorded_as_coverage_omission_and_blocks(repository):
    baseline = register(repository, BASELINE_CODE, version="1.0.0")
    candidate = register(repository, SLOW_CODE, version="1.4.0")
    report = bench(repository, timeout=0.3, repeat=1).compare(
        baseline.ref, candidate.ref, cases())
    omitted = report["coverage"]["omitted"]
    assert any(item["reason"].startswith("candidate:timeout") for item in omitted), omitted
    assert report["coverage"]["required_coverage_ok"] is False
    assert report["verdict"] == "inconclusive"
    assert report["release_decision"]["allow"] is False
    assert any("覆盖" in item for item in report["release_decision"]["blockers"])


def test_violation_is_omitted_and_blocks_release(repository):
    baseline = register(repository, BASELINE_CODE, version="1.0.0")
    candidate = register(repository, NETWORK_CODE, version="1.5.0")
    report = bench(repository, repeat=1).compare(baseline.ref, candidate.ref,
                                                 cases())
    assert report["metrics"]["violations"] >= 1
    assert any(item["reason"] == "candidate:violation"
               for item in report["coverage"]["omitted"])
    assert report["release_decision"]["allow"] is False
    assert any("越界" in item for item in report["release_decision"]["blockers"])


def test_benefit_chain_triggers_rollback_on_post_release_regression(repository):
    baseline = register(repository, BASELINE_CODE, version="1.0.0")
    candidate = register(repository, IMPROVED_CODE, version="1.1.0")
    report = bench(repository).compare(baseline.ref, candidate.ref, cases())
    healthy = benefit_chain(report, {"success_rate": 0.95, "regression": False})
    assert healthy["status"] == "complete"
    assert healthy["rollback_required"] is False
    degraded = benefit_chain(report, {"success_rate": 0.2, "regression": False})
    assert degraded["rollback_required"] is True
    assert degraded["rollback_reason"] == "发布后监测退化"
    rolled = benefit_chain(report, {"success_rate": 0.99, "regression": True})
    assert rolled["rollback_required"] is True


def test_evidence_binds_exact_candidate_version_and_digest(repository):
    baseline = register(repository, BASELINE_CODE, version="1.0.0")
    first = register(repository, IMPROVED_CODE, version="1.1.0")
    second = register(repository, REGRESSED_CODE, version="1.1.1")
    runner = bench(repository)
    first_report = runner.compare(baseline.ref, first.ref, cases())
    second_report = runner.compare(baseline.ref, second.ref, cases())
    assert first_report["candidate"]["content_digest"] != \
        second_report["candidate"]["content_digest"]
    assert first_report["evidence_digest"] != second_report["evidence_digest"]
    assert first_report["cases_digest"] == second_report["cases_digest"]


def test_invalid_configuration_and_non_code_asset_are_rejected(repository):
    baseline = register(repository, BASELINE_CODE, version="1.0.0")
    with pytest.raises(BenchError):
        HostBenchmark(IsolatedCandidateHost(repository), repeat=0)
    with pytest.raises(BenchError):
        HostBenchmark(IsolatedCandidateHost(repository), min_coverage=0)
    with pytest.raises(BenchError):
        bench(repository).compare(baseline.ref, baseline.ref, [])
    asset = repository.register(asset_id="pack.knowledge", asset_type="knowledge",
                                namespace="shared_validated", version="1.0.0",
                                content={"text": "不是代码"},
                                lifecycle_state="candidate")
    with pytest.raises(CandidateAssetError):
        bench(repository).compare(baseline.ref, asset.ref, cases())
