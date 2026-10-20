"""真实模型配置：复用 Claude 全局 settings.json（PRD 4.5 / 17）。"""

import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from evoagent.config import Config

_ENV_KEYS = (
    "EVOAGENT_PROVIDER", "EVOAGENT_API_KEY", "EVOAGENT_BASE_URL",
    "EVOAGENT_MODEL", "EVOAGENT_DATA", "EVOAGENT_CLAUDE_SETTINGS",
)


@contextmanager
def _env(**values):
    old = {k: os.environ.get(k) for k in _ENV_KEYS}
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestClaudeGlobalConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = self.root / "settings.json"
        self.settings.write_text(json.dumps({
            "env": {
                "ANTHROPIC_API_KEY": "test-key",
                "ANTHROPIC_BASE_URL": "https://gateway.example/go",
                "ANTHROPIC_MODEL": "real-model",
            }
        }), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _load(self, **env):
        base = {
            "EVOAGENT_DATA": str(self.root / "data"),
            "EVOAGENT_CLAUDE_SETTINGS": str(self.settings),
        }
        base.update(env)
        with _env(**base):
            return Config.load()

    def test_openai_provider_uses_claude_credentials(self):
        cfg = self._load(EVOAGENT_PROVIDER="openai")
        self.assertEqual(cfg.provider.api_key, "test-key")
        self.assertEqual(cfg.provider.base_url, "https://gateway.example/go/v1")
        self.assertEqual(cfg.provider.model_id, "real-model")

    def test_base_with_v1_not_duplicated(self):
        self.settings.write_text(json.dumps({"env": {
            "ANTHROPIC_API_KEY": "k",
            "ANTHROPIC_BASE_URL": "https://gateway.example/v1",
        }}), encoding="utf-8")
        cfg = self._load(EVOAGENT_PROVIDER="openai")
        self.assertEqual(cfg.provider.base_url, "https://gateway.example/v1")

    def test_explicit_env_wins(self):
        cfg = self._load(
            EVOAGENT_PROVIDER="openai",
            EVOAGENT_API_KEY="explicit-key",
            EVOAGENT_BASE_URL="https://explicit.example/v1",
            EVOAGENT_MODEL="explicit-model",
        )
        self.assertEqual(cfg.provider.api_key, "explicit-key")
        self.assertEqual(cfg.provider.base_url, "https://explicit.example/v1")
        self.assertEqual(cfg.provider.model_id, "explicit-model")

    def test_explicit_key_does_not_borrow_claude_endpoint(self):
        cfg = self._load(EVOAGENT_PROVIDER="openai",
                         EVOAGENT_API_KEY="explicit-key")
        self.assertEqual(cfg.provider.api_key, "explicit-key")
        self.assertEqual(cfg.provider.base_url, "https://api.openai.com/v1")
        self.assertEqual(cfg.provider.model_id, "gpt-4o-mini")

    def test_mock_provider_ignores_claude_settings(self):
        cfg = self._load(EVOAGENT_PROVIDER="mock")
        self.assertIsNone(cfg.provider.api_key)
        self.assertEqual(cfg.provider.model_id, "gpt-4o-mini")

    def test_missing_settings_keeps_mock_fallback(self):
        os.remove(self.settings)
        cfg = self._load(EVOAGENT_PROVIDER="openai")
        self.assertIsNone(cfg.provider.api_key)
        self.assertEqual(cfg.provider.base_url, "https://api.openai.com/v1")

    def test_malformed_settings_are_ignored(self):
        self.settings.write_text("{not json", encoding="utf-8")
        cfg = self._load(EVOAGENT_PROVIDER="openai")
        self.assertIsNone(cfg.provider.api_key)


if __name__ == "__main__":
    unittest.main()
