"""外部来源采集与引用记录（ALN-28-A）。

原则：**引用必须可访问、可复核**。每个来源记录 URL、发布者、获取时间、
内容摘要（正文 sha256）与支撑片段（quote），交付时逐条给出。

- `fetch_source`：真实 HTTP(S) 抓取，记录状态、摘要与获取时间；失败抛
  `SourceFetchError`，**不伪造可用来源**。
- `SourceRecord.accessible` 表示本次抓取是否成功；不可访问的来源不会被
  当成支撑证据。
- 夹具来源（`fixture=True`）仅用于本地对照场景，必须在交付物里明确标注。
"""

import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_UA = "EvoAgent-Verifier/1.0 (+local development)"


class SourceFetchError(RuntimeError):
    """来源不可访问或抓取失败。"""


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    url: str
    publisher: str
    retrieved_at: float
    content_digest: str
    quote: str
    claim_key: str = ""
    stance: str = "mentions"        # supports / contradicts / mentions
    trust: str = "primary"          # primary / secondary / reference
    accessible: bool = True
    fetch_error: str = ""
    fixture: bool = False

    def evidence_problem(self) -> str:
        if self.accessible is not True or type(self.fixture) is not bool:
            return "来源未确认可访问或夹具标记非法"
        if any(not isinstance(v, str) or not v.strip() for v in (self.source_id, self.publisher, self.quote)):
            return "来源身份、发布者或引用片段缺失"
        if not isinstance(self.content_digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", self.content_digest):
            return "来源正文摘要无效"
        if type(self.retrieved_at) not in (int, float) or not math.isfinite(self.retrieved_at):
            return "来源获取时间无效"
        if self.stance not in ("supports", "contradicts", "mentions") or self.trust not in ("primary", "secondary", "reference"):
            return "来源立场或类型无效"
        try:
            parsed = urllib.parse.urlsplit(self.url)
            if parsed.scheme not in (("http", "https", "fixture") if self.fixture else ("http", "https")) or not parsed.netloc:
                return "来源URL无效"
        except (TypeError, ValueError, AttributeError):
            return "来源URL无效"
        return ""

    def citation(self) -> Dict[str, Any]:
        return {"source_id": self.source_id, "url": self.url,
                "publisher": self.publisher, "retrieved_at": self.retrieved_at,
                "content_digest": self.content_digest, "quote": self.quote,
                "trust": self.trust, "fixture": self.fixture,
                "accessible": self.accessible}

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SourceRecord":
        if not isinstance(data, dict):
            raise SourceFetchError("来源记录必须为对象")
        fields = {name: data[name] for name in cls.__dataclass_fields__
                  if name in data}
        fields.setdefault("source_id", "src_" + hashlib.sha256(
            (data.get("url") if isinstance(data.get("url"), str) else "").encode()).hexdigest()[:12])
        fields.setdefault("retrieved_at", time.time())
        fields.setdefault("content_digest", "")
        fields.setdefault("quote", "")
        fields.setdefault("publisher", "")
        return cls(**fields)


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fetch_source(url: str, *, claim_key: str = "", stance: str = "mentions",
                 quote: str = "", publisher: str = "",
                 timeout: float = DEFAULT_TIMEOUT,
                 now: Optional[float] = None,
                 max_bytes: int = DEFAULT_MAX_BYTES) -> SourceRecord:
    """真实抓取并校验引用；默认只提及，supports需受信调用方明确核对语义。"""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise SourceFetchError("来源字节上限必须为正整数")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SourceFetchError("仅支持 http/https 来源: " + url)
    if stance not in ("supports", "contradicts", "mentions"):
        raise SourceFetchError("非法 stance: " + stance)
    request = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise SourceFetchError("来源内容超过字节上限")
    except (urllib.error.URLError, OSError, ValueError) as exc:  # noqa: BLE001
        raise SourceFetchError(f"抓取失败 {url}: {type(exc).__name__}: {exc}")
    if status < 200 or status >= 300:
        raise SourceFetchError(f"来源返回 HTTP {status}: {url}")
    text = raw.decode("utf-8", "replace")
    if quote and (not isinstance(quote, str) or not quote.strip()
                  or " ".join(quote.split()) not in " ".join(text.split())):
        raise SourceFetchError("引用片段不在本次抓取的正文中")
    body_digest = hashlib.sha256(raw).hexdigest()
    return SourceRecord(
        source_id="src_" + body_digest[:12] + "_" + _digest_text(url)[:6],
        url=url, publisher=publisher or parsed.hostname or url,
        retrieved_at=now if now is not None else time.time(),
        content_digest=body_digest,
        quote=quote or _snippet(text),
        claim_key=claim_key, stance=stance, accessible=True)


def _snippet(text: str, limit: int = 200) -> str:
    compact = " ".join(text.split())
    return compact[:limit]


def fixture_source(*, claim_key: str, stance: str = "supports",
                   quote: str, publisher: str = "fixture-publisher",
                   url: str = "", trust: str = "secondary",
                   accessible: bool = True,
                   content_digest: str = "") -> SourceRecord:
    """本地对照用来源（fixture=True，交付物必须标注，不得冒充真实来源）。"""
    url = url or f"fixture://{claim_key}/{publisher}/{stance}"
    digest = content_digest or _digest_text(url + "|" + quote)
    return SourceRecord(
        source_id="src_fixture_" + digest[:12], url=url,
        publisher=publisher, retrieved_at=time.time(),
        content_digest=digest, quote=quote, claim_key=claim_key,
        stance=stance, trust=trust, accessible=accessible,
        fetch_error="" if accessible else "fixture inaccessible",
        fixture=True)


def load_sources(payload: Any) -> List[SourceRecord]:
    """从 JSON 数据（列表或 {"sources": [...]}）构造来源记录。"""
    items = payload.get("sources") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise SourceFetchError("来源数据必须是数组或 {sources:[...]}")
    return [SourceRecord.from_dict(item) for item in items]
