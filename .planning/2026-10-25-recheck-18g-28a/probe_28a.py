"""ALN-28-A 独立复核探针（固定源码 a1b3a31，zcode-main，2026-10-25）。

独立编写，不复制交付测试用例。覆盖第三批指认的重点：
引用必须在抓取正文中/默认mentions/原始字节摘要/字节上限/
未来与非法时间窗/无效来源不计支持/交付守卫真实调用。
"""
import hashlib
import http.server
import json
import math
import sys
import threading
from pathlib import Path

WT = str(Path(__file__).resolve().parent / "wt")
sys.path.insert(0, WT)

from evoagent.verification.claims import Claim  # noqa: E402
from evoagent.verification.crosscheck import FactCrossChecker  # noqa: E402
from evoagent.verification.delivery import (  # noqa: E402
    DeliveryError, TrustedDelivery)
from evoagent.verification.sources import (  # noqa: E402
    SourceFetchError, SourceRecord, fetch_source, fixture_source)

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS" if cond else "FAIL"), name, ("| " + detail if detail else ""))


NOW = 1_792_900_000.0


class Handler(http.server.BaseHTTPRequestHandler):
    body = b"alpha beta gamma delta"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        pass


def start_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/doc"


# ---------- R1 引用必须在正文中 ----------
def r1(url):
    try:
        fetch_source(url, quote="epsilon zeta")
        check("R1a 引用不在正文拒绝", False, "未抛错")
    except SourceFetchError as exc:
        check("R1a 引用不在正文拒绝", "不在本次抓取的正文" in str(exc),
              str(exc)[:90])
    rec = fetch_source(url, quote="beta gamma")
    check("R1b 引用在正文接受且保留原文", rec.quote == "beta gamma", rec.quote)
    # 空白差异归一后匹配（quote 多空格）
    rec2 = fetch_source(url, quote="beta   gamma")
    check("R1c 空白归一匹配", rec2.quote == "beta   gamma")
    # 空 quote → 自动摘录正文前200字
    rec3 = fetch_source(url)
    check("R1d 空引用自动摘录", rec3.quote.startswith("alpha beta"),
          rec3.quote[:40])


# ---------- R2 默认 mentions，显式 supports 才计支持 ----------
def r2(url):
    rec = fetch_source(url, claim_key="k1", quote="alpha")
    check("R2a 默认 stance=mentions", rec.stance == "mentions", rec.stance)
    v = FactCrossChecker().verify(
        Claim("c1", "陈述", "k1"), [rec], now=NOW)
    check("R2b 仅提及 → unknown 不判可信",
          v.state == "unknown" and v.model_only is True
          and any("仅提及" in r for r in v.reasons),
          f"{v.state}|{v.reasons}")
    sup = fetch_source(url, claim_key="k2", stance="supports", quote="alpha",
                       publisher="pub-a")
    v2 = FactCrossChecker().verify(Claim("c2", "陈述", "k2"), [sup], now=NOW)
    check("R2c 显式supports → supported 0.7",
          v2.state == "supported" and v2.confidence == 0.7
          and v2.model_only is False,
          f"{v2.state}|{v2.confidence}")
    sup2 = fixture_source(claim_key="k2", stance="supports", quote="q",
                          publisher="pub-b")
    v3 = FactCrossChecker().verify(Claim("c3", "陈述", "k2"), [sup, sup2],
                                   now=NOW)
    check("R2d 双发布者置信 0.9",
          v3.state == "supported" and v3.confidence == 0.9,
          f"{v3.confidence}")


# ---------- R3 摘要为原始字节 sha256 ----------
def r3(url):
    raw = b"\xff\xfeHe\xffllo raw-bytes \x80 case"
    Handler.body = raw
    try:
        rec = fetch_source(url, quote="raw-bytes")
        digest = hashlib.sha256(raw).hexdigest()
        check("R3a 摘要=原始字节sha256", rec.content_digest == digest,
              f"{rec.content_digest[:16]}.. vs {digest[:16]}..")
        check("R3b source_id 前缀=摘要前12位",
              rec.source_id == "src_" + digest[:12] + "_" +
              hashlib.sha256(url.encode()).hexdigest()[:6], rec.source_id)
    finally:
        Handler.body = b"alpha beta gamma delta"


# ---------- R4 字节上限 ----------
def r4(url):
    Handler.body = b"x" * (2 * 1024 * 1024 + 1)
    try:
        fetch_source(url)
        check("R4a 默认2MiB上限拒绝", False, "未抛错")
    except SourceFetchError as exc:
        check("R4a 默认2MiB上限拒绝", "字节上限" in str(exc), str(exc)[:80])
    try:
        fetch_source(url, max_bytes=0)
        check("R4b 非正上限拒绝", False)
    except SourceFetchError:
        check("R4b 非正上限拒绝", True)
    finally:
        Handler.body = b"alpha beta gamma delta"


# ---------- R5 时间窗与核验时钟 ----------
def r5(url):
    sup = fetch_source(url, claim_key="kf", stance="supports", quote="alpha")
    c = Claim("cf", "陈述", "kf", valid_from=NOW + 1000)
    v = FactCrossChecker().verify(c, [sup], now=NOW)
    check("R5a 未来 valid_from → unknown 不支持",
          v.state == "unknown" and v.confidence == 0.0
          and any("尚未开始" in r for r in v.reasons),
          f"{v.state}|{v.reasons}")
    for bad, name in ((float("nan"), "R5b NaN valid_from"),
                      (float("inf"), "R5c Infinity valid_from")):
        try:
            Claim("cx", "陈述", "kf", valid_from=bad)
            check(name, False, "未抛错")
        except ValueError as exc:
            check(name, "有限" in str(exc), str(exc)[:70])
    try:
        Claim("cy", "陈述", "kf", valid_from=NOW + 10, valid_until=NOW)
        check("R5d 反向时间窗拒绝", False)
    except ValueError as exc:
        check("R5d 反向时间窗拒绝", "晚于" in str(exc), str(exc)[:70])
    try:
        FactCrossChecker().verify(c, [sup], now=float("nan"))
        check("R5e 非有限核验时钟拒绝", False)
    except ValueError as exc:
        check("R5e 非有限核验时钟拒绝", "有限" in str(exc), str(exc)[:70])
    past = Claim("cp", "陈述", "kf", valid_until=NOW - 1)
    vp = FactCrossChecker().verify(past, [sup], now=NOW)
    check("R5f 过期 → stale 优先于支持",
          vp.state == "stale" and vp.model_only is False
          and any("已过" in r for r in vp.reasons),
          f"{vp.state}|model_only={vp.model_only}")


# ---------- R6 无效/不可访问来源不计支持 ----------
def r6(url):
    dead = SourceRecord("s1", url, "pub", NOW, hashlib.sha256(b"x").hexdigest(),
                        "alpha", claim_key="k6", stance="supports",
                        accessible=False, fetch_error="500")
    v = FactCrossChecker().verify(Claim("c6", "陈述", "k6"), [dead], now=NOW)
    check("R6a 不可访问来源不入支持",
          v.state == "unknown" and len(v.inaccessible) == 1
          and any("不可访问" in r for r in v.reasons),
          f"{v.state}|inaccessible={len(v.inaccessible)}")
    broken = SourceRecord("s2", url, "", NOW, "nothex", "",
                          claim_key="k6b", stance="supports")
    v2 = FactCrossChecker().verify(Claim("c6b", "陈述", "k6b"), [broken],
                                   now=NOW)
    check("R6b 缺损记录（缺发布者/引文/摘要）不入支持",
          v2.state == "unknown" and len(v2.inaccessible) == 1, v2.state)
    bad_scheme = SourceRecord("s3", "ftp://h/d", "pub", NOW,
                              hashlib.sha256(b"x").hexdigest(), "q",
                              claim_key="k6c", stance="supports")
    v3 = FactCrossChecker().verify(Claim("c6c", "陈述", "k6c"), [bad_scheme],
                                   now=NOW)
    check("R6c 非http(s)来源不入支持", v3.state == "unknown"
          and len(v3.inaccessible) == 1, v3.state)
    # 不可访问来源的反对意见同样不计 → 不产生 contradicted
    dead_contra = SourceRecord("s4", url, "pub", NOW,
                               hashlib.sha256(b"x").hexdigest(), "alpha",
                               claim_key="k6d", stance="contradicts",
                               accessible=False)
    v4 = FactCrossChecker().verify(Claim("c6d", "陈述", "k6d"), [dead_contra],
                                   now=NOW)
    check("R6d 不可访问反对不判矛盾", v4.state == "unknown", v4.state)


# ---------- R7 交付守卫真实调用 ----------
def r7(url):
    sup = fetch_source(url, claim_key="k7", stance="supports", quote="alpha")
    claim = Claim("c7", "陈述", "k7")
    report = TrustedDelivery().build(topic="t", claims=[claim],
                                     sources=[sup], now=NOW)
    check("R7a 正常supported入 delivered 且引用完整",
          report["counts"]["delivered"] == 1
          and report["delivered"][0]["supporting"], json.dumps(report["counts"]))

    class ForgedChecker(FactCrossChecker):
        def __init__(self, verdict):
            super().__init__()
            self._verdict = verdict

        def verify(self, claim, sources, *, now=None):
            return self._verdict

    def forged(**kw):
        from evoagent.verification.crosscheck import Verdict
        base = dict(claim_id="c7f", statement="s", state="supported",
                    confidence=0.9, applicable_window={})
        base.update(kw)
        return Verdict(**base)

    cases = [
        ("R7b 无引用supported拒绝", forged(supporting=[]),
         "无引用"),
        ("R7c 无效引用supported拒绝",
         forged(supporting=[{"url": url, "publisher": "p", "quote": "q",
                             "content_digest": "zz", "accessible": True}]),
         "无效"),
        ("R7d model_only supported拒绝",
         forged(supporting=[SourceRecord(
             "s9", url, "pub", NOW, hashlib.sha256(b"x").hexdigest(),
             "alpha").citation()], model_only=True),
         "模型自述"),
    ]
    for name, verdict, frag in cases:
        td = TrustedDelivery(checker=ForgedChecker(verdict))
        try:
            td.build(topic="t", claims=[claim], sources=[sup], now=NOW)
            check(name, False, "未抛错")
        except DeliveryError as exc:
            check(name, frag in str(exc), str(exc)[:90])

    # fixture 引用必须在 citations 中带 fixture=True 标注且计数
    fx = fixture_source(claim_key="k7f", stance="supports", quote="q",
                        publisher="pub-fx")
    rep2 = TrustedDelivery().build(topic="t", claims=[Claim("c7f", "s", "k7f")],
                                   sources=[fx], now=NOW)
    check("R7e fixture引用标注并计数",
          rep2["counts"]["fixture_citations"] == 1
          and rep2["citations"][0]["fixture"] is True,
          json.dumps(rep2["counts"]))


# ---------- R8 冲突可见 ----------
def r8(url):
    sup = fetch_source(url, claim_key="k8", stance="supports", quote="alpha",
                       publisher="pub-a")
    con = fixture_source(claim_key="k8", stance="contradicts", quote="cq",
                         publisher="pub-b")
    v = FactCrossChecker().verify(Claim("c8", "陈述", "k8"), [sup, con],
                                  now=NOW)
    check("R8a 支持与反对并存 → contradicted",
          v.state == "contradicted"
          and len(v.supporting) == 1 and len(v.conflicting) == 1
          and any("来源冲突" in r for r in v.reasons),
          f"{v.state}|{v.reasons[:1]}")
    report = TrustedDelivery().build(topic="t", claims=[Claim("c8", "s", "k8")],
                                     sources=[sup, con], now=NOW)
    check("R8b 冲突进入 conflicts 且双方引用可见",
          report["counts"]["conflicts"] == 1
          and report["conflicts"][0]["supporting"]
          and report["conflicts"][0]["conflicting"]
          and {c["publisher"] for c in report["citations"]} ==
          {"pub-a", "pub-b"},
          json.dumps(report["counts"]))


def main():
    srv, url = start_server()
    try:
        for fn in (r1, r2, r3, r4, r5, r6, r7, r8):
            print(f"---- {fn.__name__} ----")
            try:
                fn(url)
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                check(fn.__name__ + "(整组执行)", False,
                      f"{type(exc).__name__}: {exc}")
    finally:
        srv.shutdown()
    fails = [r for r in results if not r[1]]
    print(f"\n=== 28-A 探针：{len(results) - len(fails)}/{len(results)} 通过 ===")
    for name, _, detail in fails:
        print("FAIL:", name, "|", detail)
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
