"""全局配置：默认值 + 环境变量覆盖。"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ModelProviderConfig:
    name: str = "mock"                       # mock | openai
    api_key: Optional[str] = None
    base_url: str = "https://api.openai.com/v1"
    model_id: str = "gpt-4o-mini"
    timeout_seconds: int = 60
    max_retries: int = 2


@dataclass
class EmbeddingConfig:
    # PRD 25：默认哈希向量；openai 使用兼容 /embeddings 端点
    provider: str = "hash"                   # hash | openai
    model_id: str = "text-embedding-3-small"
    base_url: Optional[str] = None           # 缺省复用 provider.base_url
    api_key: Optional[str] = None            # 缺省复用 provider.api_key
    timeout_seconds: int = 15


@dataclass
class BudgetConfig:
    # 默认单任务硬预算（Architecture 12.1）
    task_max_model_calls: int = 25
    task_max_tool_calls: int = 50
    task_max_cost: float = 1.00              # 计量单位（美元或等值）
    task_max_seconds: int = 300
    task_max_cost_per_model_call: float = 0.25
    task_max_tool_calls_per_tool: int = 10
    # 单工作流预算（PRD 55 / Arch 12.2）
    workflow_max_model_calls: int = 15
    workflow_max_tool_calls: int = 40
    workflow_max_cost: float = 0.60
    workflow_max_seconds: int = 180
    # 单候选实验预算（PRD 55）
    candidate_max_model_calls: int = 40
    candidate_max_tool_calls: int = 100
    candidate_max_cost: float = 2.00
    candidate_max_seconds: int = 600
    evolution_daily_cost: float = 5.00
    evolution_daily_model_calls: int = 200
    evolution_monthly_cost: float = 50.00
    evolution_monthly_model_calls: int = 2000
    # 运行时日预算池（PRD 55-58：单任务之外的整体预算）
    runtime_daily_cost: float = 20.00
    runtime_daily_model_calls: int = 2000
    # 模型提供方 / 领域专家预算池（PRD 55）
    provider_daily_cost: float = 10.00
    provider_daily_model_calls: int = 1000
    expert_daily_cost: float = 5.00
    expert_daily_model_calls: int = 500
    max_concurrent_tasks: int = 2


@dataclass
class DiagnosticsConfig:
    # PRD 43/44：诊断实验有界、可预算
    max_experiments_per_case: int = 3
    max_probe_seconds: float = 10.0
    max_daily_experiments: int = 50
    # 诊断永远不自动发布；候选生成需显式调用（PRD 45）
    auto_propose: bool = False
    # 模型辅助假设单次最多条数（Arch 9.4：仅作解释，置信度封顶 0.5）
    max_assist_hypotheses: int = 3


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("EVOAGENT_DATA", ".evoagent")))
    provider: ModelProviderConfig = field(default_factory=ModelProviderConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    diagnostics: DiagnosticsConfig = field(default_factory=DiagnosticsConfig)

    # 高风险操作默认策略：deny / confirm / allow（仅测试使用 allow）
    high_risk_mode: str = os.environ.get("EVOAGENT_HIGH_RISK", "confirm")
    # 意图识别置信度阈值（PRD 7.4）
    clarify_threshold: float = 0.5
    # ReAct 防循环（PRD 12.3）
    react_max_iterations: int = 20
    react_max_repeat_failures: int = 3
    # 上下文预算上限（Arch 8.5-8.6；实际值再受模型 context_limit 约束）
    context_max_chars: int = 6000
    # API 服务与安全控制面（PRD 83-88 / Arch 18）
    api_host: str = "127.0.0.1"
    api_port: int = 8300
    api_token: Optional[str] = os.environ.get("EVOAGENT_API_TOKEN")
    api_admin_token: Optional[str] = os.environ.get("EVOAGENT_ADMIN_TOKEN")
    release_signing_secret: Optional[str] = os.environ.get(
        "EVOAGENT_RELEASE_SECRET")

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        cfg = cls()
        if path and Path(path).exists():
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if "provider" in data:
                for k, v in data["provider"].items():
                    if hasattr(cfg.provider, k):
                        setattr(cfg.provider, k, v)
            if "embedding" in data:
                for k, v in data["embedding"].items():
                    if hasattr(cfg.embedding, k):
                        setattr(cfg.embedding, k, v)
            if "budget" in data:
                for k, v in data["budget"].items():
                    if hasattr(cfg.budget, k):
                        setattr(cfg.budget, k, v)
            if "diagnostics" in data:
                for k, v in data["diagnostics"].items():
                    if hasattr(cfg.diagnostics, k):
                        setattr(cfg.diagnostics, k, v)
        if os.environ.get("EVOAGENT_PROVIDER"):
            cfg.provider.name = os.environ["EVOAGENT_PROVIDER"]
        # 显式 provider 选择真实模型时，复用 Claude 全局配置中的凭据与端点
        if cfg.provider.name == "openai":
            _apply_claude_global(cfg.provider)
        # 环境变量覆盖（优先级最高）
        if os.environ.get("EVOAGENT_API_KEY"):
            cfg.provider.api_key = os.environ["EVOAGENT_API_KEY"]
        if os.environ.get("EVOAGENT_BASE_URL"):
            cfg.provider.base_url = os.environ["EVOAGENT_BASE_URL"]
        if os.environ.get("EVOAGENT_MODEL"):
            cfg.provider.model_id = os.environ["EVOAGENT_MODEL"]
        if os.environ.get("EVOAGENT_EMBEDDING_PROVIDER"):
            cfg.embedding.provider = os.environ["EVOAGENT_EMBEDDING_PROVIDER"]
        if os.environ.get("EVOAGENT_EMBEDDING_MODEL"):
            cfg.embedding.model_id = os.environ["EVOAGENT_EMBEDDING_MODEL"]
        if os.environ.get("EVOAGENT_EMBEDDING_BASE_URL"):
            cfg.embedding.base_url = os.environ["EVOAGENT_EMBEDDING_BASE_URL"]
        if os.environ.get("EVOAGENT_EMBEDDING_API_KEY"):
            cfg.embedding.api_key = os.environ["EVOAGENT_EMBEDDING_API_KEY"]
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        return cfg


_DEFAULT_OPENAI_BASE = "https://api.openai.com/v1"
_DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


def _apply_claude_global(provider: ModelProviderConfig) -> None:
    """从 Claude 全局 settings.json 补充缺失的真实模型配置。

    Claude 的网关同时提供 OpenAI 兼容端点；base 自动补 `/v1`。
    仅填充未显式配置的字段，且任何失败都静默忽略（不阻断 Mock 演示）。
    """
    if provider.api_key or os.environ.get("EVOAGENT_API_KEY"):
        return
    path = os.environ.get("EVOAGENT_CLAUDE_SETTINGS") or str(
        Path.home() / ".claude" / "settings.json")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        env = data.get("env") or {}
    except (OSError, ValueError, AttributeError):
        return
    if not isinstance(env, dict):
        return
    key = env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN")
    if not key:
        return
    provider.api_key = key
    base = env.get("ANTHROPIC_BASE_URL")
    if base and provider.base_url == _DEFAULT_OPENAI_BASE:
        provider.base_url = _openai_base_url(base)
    model = (env.get("ANTHROPIC_MODEL")
             or env.get("ANTHROPIC_DEFAULT_SONNET_MODEL"))
    if model and provider.model_id == _DEFAULT_OPENAI_MODEL:
        provider.model_id = model


def _openai_base_url(anthropic_base: str) -> str:
    base = anthropic_base.rstrip("/")
    return base if base.endswith("/v1") else base + "/v1"
