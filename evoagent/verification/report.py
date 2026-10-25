"""可信交付示例与实测证据（ALN-28-A）。

`python -m evoagent.verification.report --out <path>`：
1. **真实 HTTP 抓取**一个公网来源（example.com），记录可达性、延迟、正文摘要；
2. 用「本地对照夹具（fixture）」跑五态判定矩阵：supported / contradicted /
   inferred / unknown / stale；
3. 生成 `TrustedDelivery` 交付报告：只有带可访问引用的结论进入 delivered，
   冲突与未核验结论逐条可见；
4. 输出 JSON 证据并返回退出码（矩阵与预期不符则非 0）。

夹具来源在交付物里带 `fixture=True` 明确标注，不冒充真实外部来源。
"""

import argparse
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .claims import Claim
from .crosscheck import FactCrossChecker
from .delivery import TrustedDelivery
from .sources import SourceFetchError, SourceRecord, fetch_source, fixture_source


def build_matrix(now: float) -> Dict[str, Any]:
    sources: List[SourceRecord] = [
        # supported：两个不同发布者的可访问夹具来源
        fixture_source(claim_key="k_supported", stance="supports",
                       publisher="fixture-a", quote="夹具：结论 A 成立（来源一）"),
        fixture_source(claim_key="k_supported", stance="supports",
                       publisher="fixture-b", quote="夹具：结论 A 成立（来源二）"),
        # contradicted：一条支持、一条反对
        fixture_source(claim_key="k_conflict", stance="supports",
                       publisher="fixture-c", quote="夹具：结论 B 成立"),
        fixture_source(claim_key="k_conflict", stance="contradicts",
                       publisher="fixture-d", quote="夹具：结论 B 不成立"),
        # stale：有支持来源但适用时间已过
        fixture_source(claim_key="k_stale", stance="supports",
                       publisher="fixture-e", quote="夹具：结论 C 在旧窗口成立"),
        # mentions：只提及、未支持
        fixture_source(claim_key="k_mentions", stance="mentions",
                       publisher="fixture-f", quote="夹具：提到主题 D，但未下结论"),
        # unknown：不可访问来源
        fixture_source(claim_key="k_unreachable", stance="supports",
                       publisher="fixture-g", quote="夹具：不可访问来源",
                       accessible=False),
    ]
    claims = [
        Claim(claim_id="c_supported", statement="结论 A 成立", claim_key="k_supported"),
        Claim(claim_id="c_conflict", statement="结论 B 成立", claim_key="k_conflict"),
        Claim(claim_id="c_inferred", statement="结论 I 由内部事实推导",
              claim_key="k_inferred", derived_from_internal=True),
        Claim(claim_id="c_unknown", statement="模型自述结论 U 成立",
              claim_key="k_unknown"),
        Claim(claim_id="c_stale", statement="结论 C 成立", claim_key="k_stale",
              valid_until=now - 3600),
        Claim(claim_id="c_mentions", statement="结论 D 成立", claim_key="k_mentions"),
        Claim(claim_id="c_unreachable", statement="结论 G 成立",
              claim_key="k_unreachable"),
    ]
    checker = FactCrossChecker()
    verdicts = {claim.claim_id: checker.verify(claim, sources, now=now).to_dict()
                for claim in claims}
    delivery = TrustedDelivery(checker=checker).build(
        topic="ALN-28-A 本地对照矩阵", claims=claims, sources=sources, now=now)
    return {"sources_fixture": [s.to_dict() for s in sources],
            "verdicts": verdicts, "delivery": delivery,
            "expected_states": {
                "c_supported": "supported", "c_conflict": "contradicted",
                "c_inferred": "inferred", "c_unknown": "unknown",
                "c_stale": "stale", "c_mentions": "unknown",
                "c_unreachable": "unknown"}}


def real_fetch_probe(now: float) -> Dict[str, Any]:
    url = "https://example.com/"
    started = time.time()
    try:
        record = fetch_source(url, claim_key="k_real_page",
                              stance="supports",
                              quote="Example Domain", now=now)
    except SourceFetchError as exc:
        return {"url": url, "accessible": False,
                "error": f"{type(exc).__name__}: {exc}",
                "measured_at": now}
    return {"url": url, "accessible": record.accessible,
            "latency_s": round(time.time() - started, 3),
            "status_ok": True, "content_digest": record.content_digest,
            "publisher": record.publisher, "quote": record.quote,
            "fixture": record.fixture, "measured_at": now}


def real_claim_delivery(now: float, fetch_probe: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """真实来源支撑的结论：引用片段必须来自本次真实抓取。"""
    if not fetch_probe.get("accessible"):
        return None
    source = SourceRecord(
        source_id="src_real_example", url=fetch_probe["url"],
        publisher=fetch_probe.get("publisher") or "example.com",
        retrieved_at=now, content_digest=fetch_probe["content_digest"],
        quote=fetch_probe.get("quote") or "Example Domain",
        claim_key="k_real_page", stance="supports", trust="primary")
    claim = Claim(claim_id="c_real_page",
                  statement="example.com 页面包含 “Example Domain” 文案",
                  claim_key="k_real_page")
    return TrustedDelivery().build(
        topic="真实来源引用示例", claims=[claim], sources=[source], now=now)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ALN-28-A 核验矩阵实测")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    now = time.time()
    fetch_probe = real_fetch_probe(now)
    matrix = build_matrix(now)
    real = real_claim_delivery(now, fetch_probe)
    verdicts = matrix["verdicts"]
    ok = all(verdicts[claim_id]["state"] == expected
             for claim_id, expected in matrix["expected_states"].items())
    ok = ok and (real is None or real["delivered"][0]["supporting"][0]["fixture"] is False)
    report = {
        "task": "ALN-28-A 外部事实核验与可信交付",
        "measured_at": now,
        "real_fetch": fetch_probe,
        "real_source_delivery": real,
        "fixture_matrix": matrix,
        "verdict_ok": bool(ok),
    }
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
