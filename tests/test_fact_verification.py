"""ALN-28-A：外部事实核验与可信交付测试。

验收：引用可访问且支持结论；来源冲突可见；无外部证据不得仅凭模型自述判可信。
真实网络用例在不可用时跳过（不把网络失败当通过）。
"""

import json

import pytest

from evoagent.verification import (Claim, ClaimState, DeliveryError,
                                   FactCrossChecker, SourceFetchError,
                                   SourceRecord, TrustedDelivery, fetch_source,
                                   fixture_source)
from evoagent.verification.crosscheck import Verdict
from evoagent.verification.report import main as report_main


def checker():
    return FactCrossChecker()


def test_real_http_source_is_accessible_and_cited():
    try:
        record = fetch_source("https://example.com/", claim_key="k_page",
                              quote="Example Domain", timeout=10)
    except SourceFetchError as exc:  # 网络不可用 → 明确跳过，不伪造通过
        pytest.skip("公网不可达，跳过真实抓取用例: " + str(exc))
    assert record.accessible is True
    assert record.fixture is False
    assert record.url == "https://example.com/"
    assert len(record.content_digest) == 64
    assert record.quote
    citation = record.citation()
    assert citation["url"] == record.url
    assert citation["content_digest"] == record.content_digest


def test_two_publishers_make_supported_with_higher_confidence():
    claim = Claim(claim_id="c1", statement="A 成立", claim_key="k")
    sources = [fixture_source(claim_key="k", publisher="pub-a", quote="A 成立"),
               fixture_source(claim_key="k", publisher="pub-b", quote="A 成立")]
    verdict = checker().verify(claim, sources, now=1000.0)
    assert verdict.state == ClaimState.SUPPORTED.value
    assert verdict.confidence == 0.9
    assert len(verdict.supporting) == 2
    assert verdict.model_only is False
    assert all(c["url"] and c["content_digest"] for c in verdict.supporting)
    assert "可访问来源直接支持" in " ".join(verdict.reasons)


def test_single_publisher_confidence_lower():
    claim = Claim(claim_id="c1", statement="A 成立", claim_key="k")
    source = fixture_source(claim_key="k", publisher="pub-a", quote="A 成立")
    verdict = checker().verify(claim, [source], now=1000.0)
    assert verdict.state == "supported" and verdict.confidence == 0.7


def test_conflict_is_visible_with_both_sides():
    claim = Claim(claim_id="c2", statement="B 成立", claim_key="k")
    sources = [fixture_source(claim_key="k", stance="supports", publisher="a",
                              quote="B 成立"),
               fixture_source(claim_key="k", stance="contradicts", publisher="b",
                              quote="B 不成立")]
    verdict = checker().verify(claim, sources, now=1000.0)
    assert verdict.state == ClaimState.CONTRADICTED.value
    assert len(verdict.supporting) == 1 and len(verdict.conflicting) == 1
    assert any("来源冲突" in reason for reason in verdict.reasons)


def test_model_only_claim_never_trusted():
    claim = Claim(claim_id="c3", statement="模型说 U 成立", claim_key="k")
    verdict = checker().verify(claim, [], now=1000.0)
    assert verdict.state == ClaimState.UNKNOWN.value
    assert verdict.model_only is True
    assert verdict.supporting == []
    assert "不得判为可信" in " ".join(verdict.reasons)


def test_expired_claim_is_stale_even_with_support():
    claim = Claim(claim_id="c4", statement="C 成立", claim_key="k",
                  valid_until=900.0)
    source = fixture_source(claim_key="k", quote="C 成立")
    verdict = checker().verify(claim, [source], now=1000.0)
    assert verdict.state == ClaimState.STALE.value
    assert verdict.supporting
    assert any("适用时间已过" in reason for reason in verdict.reasons)
    assert verdict.applicable_window["valid_until"] == 900.0


def test_inferred_from_internal_is_not_supported():
    claim = Claim(claim_id="c5", statement="I 由内部事实推导", claim_key="k",
                  derived_from_internal=True)
    verdict = checker().verify(claim, [], now=1000.0)
    assert verdict.state == ClaimState.INFERRED.value
    assert verdict.state != "supported"
    assert "内部已核验事实推导" in " ".join(verdict.reasons)


def test_mention_without_support_stays_unknown():
    claim = Claim(claim_id="c6", statement="D 成立", claim_key="k")
    source = fixture_source(claim_key="k", stance="mentions", quote="提到 D")
    verdict = checker().verify(claim, [source], now=1000.0)
    assert verdict.state == "unknown"
    assert verdict.mentions
    assert "仅提及" in " ".join(verdict.reasons)


def test_inaccessible_source_not_counted():
    claim = Claim(claim_id="c7", statement="G 成立", claim_key="k")
    source = fixture_source(claim_key="k", accessible=False, quote="G 成立")
    verdict = checker().verify(claim, [source], now=1000.0)
    assert verdict.state == "unknown"
    assert verdict.supporting == []
    assert len(verdict.inaccessible) == 1
    assert any("不可访问" in reason for reason in verdict.reasons)


def test_delivery_only_delivers_supported_and_marks_fixtures():
    now = 1000.0
    claims = [Claim(claim_id="ok", statement="A", claim_key="k1"),
              Claim(claim_id="bad", statement="U", claim_key="k2")]
    sources = [fixture_source(claim_key="k1", quote="A 成立",
                              publisher="fixture-x")]
    report = TrustedDelivery().build(topic="t", claims=claims, sources=sources,
                                     now=now)
    assert [v["claim_id"] for v in report["delivered"]] == ["ok"]
    assert [v["claim_id"] for v in report["unverified"]] == ["bad"]
    assert report["counts"]["fixture_citations"] == 1
    assert report["citations"][0]["fixture"] is True
    assert report["rules"]["model_only_trust"] is False
    assert "不得仅凭模型自述" in report["policy"]


def test_delivery_guard_rejects_supported_without_citation():
    empty = Verdict(claim_id="x", statement="s", state="supported",
                    confidence=0.9)
    with pytest.raises(DeliveryError):
        TrustedDelivery.assert_deliverable(empty)
    model_only = Verdict(claim_id="y", statement="s", state="supported",
                         confidence=0.9, model_only=True,
                         supporting=[{"url": "u", "content_digest": "d"}])
    with pytest.raises(DeliveryError):
        TrustedDelivery.assert_deliverable(model_only)


def test_report_cli_writes_matrix_evidence(tmp_path, monkeypatch):
    # CLI/matrix is deterministic offline; public fetch has its own separate test.
    def unavailable(*args, **kwargs):
        raise SourceFetchError("offline test source unavailable")
    monkeypatch.setattr("evoagent.verification.report.fetch_source", unavailable)
    out = tmp_path / "evidence.json"
    code = report_main(["--out", str(out)])
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["verdict_ok"] is True
    assert data["real_fetch"]["accessible"] is False
    assert data["real_source_delivery"] is None
    states = {cid: v["state"] for cid, v in data["fixture_matrix"]["verdicts"].items()}
    assert states["c_supported"] == "supported"
    assert states["c_conflict"] == "contradicted"
    assert states["c_inferred"] == "inferred"
    assert states["c_unknown"] == "unknown"
    assert states["c_stale"] == "stale"
    assert states["c_mentions"] == "unknown"
    assert states["c_unreachable"] == "unknown"
    delivery = data["fixture_matrix"]["delivery"]
    assert delivery["rules"]["citation_accessible_required"] is True
    assert delivery["counts"]["conflicts"] == 1
