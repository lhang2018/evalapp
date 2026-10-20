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
class BudgetConfig:
    # 默认单任务硬预算（Architecture 12.1）
    task_max_model_calls: int = 25
    task_max_tool_calls: int = 50
    task_max_cost: float = 1.00              # 计量单位（美元或等值）
    task_max_seconds: int = 300
    evolution_daily_cost: float = 5.00
    evolution_daily_model_calls: int = 200
    # 运行时日预算池（PRD 55-58：单任务之外的整体预算）
    runtime_daily_cost: float = 20.00
    runtime_daily_model_calls: int = 2000
    max_concurrent_tasks: int = 2


@dataclass
class DiagnosticsConfig:
    # PRD 43/44：诊断实验有界、可预算
    max_experiments_per_case: int = 3
    max_probe_seconds: float = 10.0
    max_daily_experiments: int = 50
    # 诊断永远不自动发布；候选生成需显式调用（PRD 45）
    auto_propose: bool = False


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("EVOAGENT_DATA", ".evoagent")))
    provider: ModelProviderConfig = field(default_factory=ModelProviderConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    diagnostics: DiagnosticsConfig = field(default_factory=DiagnosticsConfig)

    # 高风险操作默认策略：deny / confirm / allow（仅测试使用 allow）
    high_risk_mode: str = os.environ.get("EVOAGENT_HIGH_RISK", "confirm")
    # 意图识别置信度阈值（PRD 7.4）
    clarify_threshold: float = 0.5
    # ReAct 防循环（PRD 12.3）
    react_max_iterations: int = 20
    react_max_repeat_failures: int = 3
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
            if "budget" in data:
                for k, v in data["budget"].items():
                    if hasattr(cfg.budget, k):
                        setattr(cfg.budget, k, v)
            if "diagnostics" in data:
                for k, v in data["diagnostics"].items():
                    if hasattr(cfg.diagnostics, k):
                        setattr(cfg.diagnostics, k, v)
        # 环境变量覆盖
        if os.environ.get("EVOAGENT_PROVIDER"):
            cfg.provider.name = os.environ["EVOAGENT_PROVIDER"]
        if os.environ.get("EVOAGENT_API_KEY"):
            cfg.provider.api_key = os.environ["EVOAGENT_API_KEY"]
        if os.environ.get("EVOAGENT_BASE_URL"):
            cfg.provider.base_url = os.environ["EVOAGENT_BASE_URL"]
        if os.environ.get("EVOAGENT_MODEL"):
            cfg.provider.model_id = os.environ["EVOAGENT_MODEL"]
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        return cfg
