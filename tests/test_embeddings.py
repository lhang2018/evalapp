"""记忆向量化（PRD 25）：真实嵌入提供方、持久化、回退与配置。"""

import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from evoagent.config import Config, EmbeddingConfig, ModelProviderConfig
from evoagent.memory.embeddings import (HashEmbeddingProvider,
                                        OpenAIEmbeddingProvider, build_embedder)
from evoagent.memory.store import MemoryItem, MemoryStore


class KeywordEmbedding:
    """确定性语义替身：语义相近的文本得到同一向量，词形可以完全不同。"""

    model_id = "fake-semantic"

    def __init__(self):
        self.batches = []

    def embed(self, texts):
        self.batches.append(list(texts))
        out = []
        for text in texts:
            if "茶" in text or "tea" in text:
                out.append([1.0, 0.0, 0.0])
            elif "车" in text or "car" in text:
                out.append([0.0, 1.0, 0.0])
            else:
                out.append([0.0, 0.0, 1.0])
        return out


class FailingEmbedding:
    model_id = "failing"

    def embed(self, texts):
        raise RuntimeError("嵌入服务不可用")


class TestEmbeddings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_embed_")
        self.db = os.path.join(self.tmp, "memory.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_semantic_recall_without_lexical_overlap(self):
        provider = KeywordEmbedding()
        store = MemoryStore(self.db, embedder=provider)
        item = store.write(MemoryItem(content="如何泡茶"))
        rows = store.search("tea")
        self.assertEqual([m.memory_id for m in rows], [item.memory_id])
        store.close()

    def test_vectors_are_persisted_and_reused(self):
        provider = KeywordEmbedding()
        store = MemoryStore(self.db, embedder=provider)
        item = store.write(MemoryItem(content="如何泡茶"))
        self.assertTrue(provider.batches)  # 写入时已向量化

        provider.batches.clear()
        second = MemoryStore(self.db, embedder=provider)
        rows = second.search("tea")
        self.assertEqual([m.memory_id for m in rows], [item.memory_id])
        # 只嵌入查询；已存向量不重复计算
        self.assertEqual(provider.batches, [["tea"]])

        row = second._conn.execute(
            "SELECT model, dim FROM memory_vectors WHERE memory_id=?",
            (item.memory_id,)).fetchone()
        self.assertEqual(row["model"], "fake-semantic")
        self.assertEqual(row["dim"], 3)
        second.close()
        store.close()

    def test_provider_failure_falls_back_to_hash(self):
        store = MemoryStore(self.db, embedder=FailingEmbedding())
        item = store.write(MemoryItem(content="上海办公地址"))
        rows = store.search("地址")
        self.assertIn(item.memory_id, [m.memory_id for m in rows])
        row = store._conn.execute(
            "SELECT model FROM memory_vectors WHERE memory_id=?",
            (item.memory_id,)).fetchone()
        self.assertEqual(row["model"], "hash-64")
        store.close()

    def test_model_change_reembeds_existing_items(self):
        store = MemoryStore(self.db, embedder=KeywordEmbedding())
        item = store.write(MemoryItem(content="如何泡茶"))
        store.close()

        hashed = MemoryStore(self.db)  # 换回哈希模型
        self.assertIn(item.memory_id,
                      [m.memory_id for m in hashed.search("泡茶")])
        row = hashed._conn.execute(
            "SELECT model FROM memory_vectors WHERE memory_id=?",
            (item.memory_id,)).fetchone()
        self.assertEqual(row["model"], "hash-64")
        hashed.close()

    def test_correct_and_delete_maintain_vectors(self):
        store = MemoryStore(self.db, embedder=KeywordEmbedding())
        item = store.write(MemoryItem(content="如何泡茶"))

        store.correct(item.memory_id, "如何开车")
        row = store._conn.execute(
            "SELECT vector FROM memory_vectors WHERE memory_id=?",
            (item.memory_id,)).fetchone()
        self.assertEqual(json.loads(row["vector"]), [0.0, 1.0, 0.0])

        self.assertTrue(store.delete(item.memory_id))
        row = store._conn.execute(
            "SELECT 1 FROM memory_vectors WHERE memory_id=?",
            (item.memory_id,)).fetchone()
        self.assertIsNone(row)
        store.close()

    def test_build_embedder_requires_credentials(self):
        cfg = SimpleNamespace(
            embedding=EmbeddingConfig(provider="openai", model_id="emb-1"),
            provider=ModelProviderConfig(name="mock", api_key=None))
        self.assertIsInstance(build_embedder(cfg), HashEmbeddingProvider)

        cfg.provider.api_key = "secret"
        cfg.provider.base_url = "http://localhost:9999/v1"
        provider = build_embedder(cfg)
        self.assertIsInstance(provider, OpenAIEmbeddingProvider)
        self.assertEqual(provider.model_id, "emb-1")

    def test_config_loads_embedding_env_and_file(self):
        cfg_path = os.path.join(self.tmp, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump({"embedding": {"provider": "openai",
                                     "model_id": "file-embed"}}, fh)
        with mock.patch.dict(os.environ, {
                "EVOAGENT_DATA": os.path.join(self.tmp, "data"),
                "EVOAGENT_EMBEDDING_PROVIDER": "openai",
                "EVOAGENT_EMBEDDING_MODEL": "env-embed",
                "EVOAGENT_EMBEDDING_API_KEY": "k",
        }, clear=False):
            cfg = Config.load(cfg_path)
        self.assertEqual(cfg.embedding.provider, "openai")
        self.assertEqual(cfg.embedding.model_id, "env-embed")
        self.assertEqual(cfg.embedding.api_key, "k")

    def test_openai_embedding_request_shape(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"data": [
                    {"index": 0, "embedding": [3.0, 4.0]}]}).encode()

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            captured["timeout"] = timeout
            return FakeResponse()

        provider = OpenAIEmbeddingProvider("emb-1", "secret",
                                           "http://localhost:1/v1",
                                           timeout_seconds=7)
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            vector = provider.embed(["hello"])[0]
        self.assertEqual(captured["url"],
                         "http://localhost:1/v1/embeddings")
        self.assertEqual(captured["body"],
                         {"model": "emb-1", "input": ["hello"]})
        self.assertEqual(captured["timeout"], 7)
        self.assertEqual(vector, [0.6, 0.8])


if __name__ == "__main__":
    unittest.main()
