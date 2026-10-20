"""记忆向量化（PRD 25）。

默认零依赖哈希向量；配置为 openai 时调用 OpenAI 兼容 `/embeddings`。
嵌入服务失败时由 MemoryStore 回退哈希向量，检索不因外部服务中断而失败。
"""

import json
import math
import urllib.request
import zlib
from typing import Any, List, Protocol


class EmbeddingProvider(Protocol):
    model_id: str

    def embed(self, texts: List[str]) -> List[List[float]]:
        ...


class HashEmbeddingProvider:
    """词形哈希到固定维度；无外部依赖，作为默认与回退实现。"""

    model_id = "hash-64"

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [hash_vector(text) for text in texts]


class OpenAIEmbeddingProvider:
    """OpenAI 兼容嵌入端点（`POST {base_url}/embeddings`）。"""

    def __init__(self, model_id: str, api_key: str, base_url: str,
                 timeout_seconds: int = 15):
        self.model_id = model_id
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def embed(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        payload = {"model": self.model_id, "input": list(texts)}
        req = urllib.request.Request(
            self._base_url + "/embeddings",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._api_key}"})
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rows = sorted(data.get("data") or [],
                      key=lambda row: row.get("index", 0))
        vectors = [normalize([float(x) for x in row.get("embedding") or []])
                   for row in rows]
        if len(vectors) != len(texts) or any(not v for v in vectors):
            raise ValueError("嵌入返回数量或维度不合法")
        return vectors


def build_embedder(config: Any) -> EmbeddingProvider:
    """按配置构造嵌入提供方；缺少凭据或配置时退回哈希向量。"""
    cfg = getattr(config, "embedding", None)
    if cfg is None or getattr(cfg, "provider", "hash") != "openai":
        return HashEmbeddingProvider()
    api_key = getattr(cfg, "api_key", None) or getattr(
        getattr(config, "provider", None), "api_key", None)
    if not api_key:
        return HashEmbeddingProvider()
    base_url = getattr(cfg, "base_url", None) or getattr(
        getattr(config, "provider", None), "base_url", None)
    if not base_url:
        return HashEmbeddingProvider()
    return OpenAIEmbeddingProvider(
        model_id=getattr(cfg, "model_id", "text-embedding-3-small"),
        api_key=api_key, base_url=base_url,
        timeout_seconds=getattr(cfg, "timeout_seconds", 15))


_VECTOR_DIM = 64


def hash_vector(text: str) -> List[float]:
    """稳定哈希词向量：不是语义嵌入，用于回退与默认模式。"""
    vec = [0.0] * _VECTOR_DIM
    for token in words(text):
        vec[zlib.crc32(token.encode("utf-8")) % _VECTOR_DIM] += 1.0
    return normalize(vec)


def normalize(vec: List[float]) -> List[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else list(vec)


def words(text: str) -> List[str]:
    """混合分词：拉丁按词；中文用 bigram（朴素但够用于重叠记分）。"""
    import re
    raw = re.findall(r"[A-Za-z0-9]{2,}|[一-鿿]+", text.lower())
    out: List[str] = []
    for token in raw:
        if token.isascii():
            out.append(token)
        elif len(token) <= 2:
            out.append(token)
        else:
            out.extend(token[i:i + 2] for i in range(len(token) - 1))
    return out
