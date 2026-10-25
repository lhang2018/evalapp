"""ALN-29-D：用户可核验的改进效果与收益说明测试。

验收：说明区分候选/发布/观测结果；无真实对照不声称收益；退化与限制可见。
"""

import json
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.evolution.benefit_statement import build_user_benefit_statement


def candidate(**overrides):
    values = dict(candidate_id="cand_1", status="CANDIDATE_CREATED",
                  target_component="routing/model_pick",
                  target_problem="分类过慢", benefit_result=None,
                  quality_result=None, monitor_result=None,
                  evaluation_report_id="", deployed_release_id="",
                  rollback_version="v0")
    values.update(overrides)
    return SimpleNamespace(**values)


def benchmark(**overrides):
    values = dict(verdict="improved", reproducible=True,
                  metrics={"success_delta": 0.1},
                  evidence_digest="bench-digest", benchmark_version="host-bench/1")
    values.update(overrides)
    return values


def test_candidate_only_claims_nothing_beyond_proposal():
    statement = build_user_benefit_statement(candidate())
    assert statement["scope"] == "candidate-only"
    assert all(item["basis"] == "candidate" for item in statement["claims"])
    reasons = {item["reason"] for item in statement["cannot_claim"]}
    assert any("没有对照评测证据" in reason for reason in reasons)
    assert any("发布后监测" in reason for reason in reasons)
    assert statement["degradations"] == []
    assert "候选" in statement["summary"]


def test_reproducible_benchmark_improvement_is_scoped_to_benchmark():
    statement = build_user_benefit_statement(
        candidate(quality_result={"report_id": "eval-1"}),
        benchmark_report=benchmark())
    assert statement["scope"] == "benchmark-only"
    bench_claims = [item for item in statement["claims"]
                    if item["basis"] == "benchmark"]
    assert bench_claims and bench_claims[0]["verifiable"] is True
    assert "Eval-1" not in json.dumps(statement)  # 不伪造报告 id 大小写
    assert "eval-1" in json.dumps(bench_claims[0]["evidence_refs"])
    assert "不等于线上收益" in statement["summary"]
    assert any("线上收益" in item["reason"] for item in statement["cannot_claim"])


def test_regressed_benchmark_is_degradation_and_no_positive_claim():
    statement = build_user_benefit_statement(
        candidate(), benchmark_report=benchmark(verdict="regressed"))
    assert statement["scope"] == "candidate-only"
    kinds = {item["kind"] for item in statement["degradations"]}
    assert "benchmark_regression" in kinds
    assert not any(item["basis"] == "benchmark" and item["verifiable"]
                   for item in statement["claims"])
    assert any("regressed" in item["reason"] for item in statement["cannot_claim"])


def test_non_reproducible_benchmark_cannot_claim_benefit():
    statement = build_user_benefit_statement(
        candidate(), benchmark_report=benchmark(reproducible=False))
    assert any("不可复现" in item["reason"] for item in statement["cannot_claim"])
    assert not any(item["basis"] == "benchmark" for item in statement["claims"])


def test_post_release_monitor_unlocks_online_claim_and_surfaces_warnings():
    statement = build_user_benefit_statement(
        candidate(monitor_result={"decision": "keep",
                                  "metrics": {"success_rate": 0.97},
                                  "warnings": ["样本量偏小"], "monitor_id": "mon-1"}))
    assert statement["scope"] == "release-monitored"
    observation = [item for item in statement["claims"]
                   if item["basis"] == "monitor"]
    assert observation and observation[0]["verifiable"] is True
    assert any("样本量偏小" in item for item in statement["limitations"])


def test_monitor_rollback_blocks_benefit_claim_with_visible_degradation():
    statement = build_user_benefit_statement(
        candidate(monitor_result={"decision": "rollback_recommended",
                                  "reasons": ["成功率下降"], "metrics": {}}))
    kinds = {item["kind"] for item in statement["degradations"]}
    assert "post_release_regression" in kinds
    assert any("不能声称线上收益" in item["reason"]
               for item in statement["cannot_claim"])
    assert statement["scope"] == "candidate-only"


def test_release_stage_is_separerated_from_observation():
    statement = build_user_benefit_statement(
        candidate(deployed_release_id="rel-9"))
    assert statement["stages"]["release"]["release_id"] == "rel-9"
    assert any(item["basis"] == "release" for item in statement["claims"])
    assert any("不等于线上有效" in item for item in statement["limitations"])
    assert statement["scope"] == "candidate-only"   # 发布 ≠ 观测


def test_degraded_dimension_is_listed():
    statement = build_user_benefit_statement(
        candidate(benefit_result={"dimensions": {
            "quality": {"score": 0.6, "degraded": True},
            "cost": {"score": 0.9}}}))
    kinds = {item["kind"] for item in statement["degradations"]}
    assert "dimension_degraded" in kinds


# ---------------- HTTP 端点 ----------------

def call(base, path, token="root"):
    request = urllib.request.Request(base + path, headers={
        "Authorization": f"Bearer {token}"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_api_returns_statement_for_real_candidate(tmp_path):
    app = App(data_dir=str(tmp_path))
    app.config.api_token = "op"
    app.config.api_admin_token = "root"
    server = ApiServer(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.port}"
    try:
        cand = app.evolution.propose_candidate(
            "v0", "routing/model_pick", "分类过慢", "提高并发度",
            change_scope="local")
        status, payload = call(base,
                               f"/evolution/candidates/{cand.candidate_id}"
                               "/benefit-statement")
        assert status == 200
        assert payload["candidate_id"] == cand.candidate_id
        assert payload["scope"] == "candidate-only"
        assert payload["stages"]["candidate"]["status"]
        assert payload["cannot_claim"]
        status, _ = call(base, "/evolution/candidates/nope/benefit-statement")
        assert status == 404
    finally:
        server.shutdown()
        app.db.close()
