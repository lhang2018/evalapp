"""模型适配层（Architecture 7.1 / PRD 18）。

契约：适配器只负责「给出行动/答案建议」，执行权始终在运行时控制器。
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


@dataclass
class ModelProfile:
    """模型档案（PRD 18 模型注册表条目）。"""
    model_id: str
    provider: str = "unknown"
    api_version: str = ""
    context_limit: int = 8000
    supports_tool_call: bool = True
    supports_structured_output: bool = True
    supports_vision: bool = False
    rpm_limit: int = 60
    price_per_1k_input: float = 0.0
    price_per_1k_output: float = 0.0
    # 路由评分维度（Arch 7：质量/成本/时延/可用性）
    quality_score: float = 0.7
    avg_latency_s: float = 1.0
    available: bool = True
    known_limitations: List[str] = field(default_factory=list)
    adapter_config: Dict[str, Any] = field(default_factory=dict)
    compat_tested: bool = False
    last_verified_at: float = 0.0


@dataclass
class Action:
    """模型提出的行动建议 —— 不具备执行权（PRD 12.4）。"""
    kind: str                                   # tool_call | finish | request_user
    tool: Optional[str] = None
    args: Dict[str, Any] = field(default_factory=dict)
    answer: Optional[str] = None                # finish 时的结论
    reasoning: str = ""
    confidence: float = 0.8


@dataclass
class ChatMessage:
    role: str            # system | user | assistant | tool
    content: str
    name: Optional[str] = None


class ModelAdapter(Protocol):
    """所有大语言模型通过统一适配接口接入。"""

    def profile(self) -> ModelProfile: ...
    def suggest_action(self, messages: List[ChatMessage],
                       available_tools: List[Dict[str, Any]]) -> Action: ...
    def generate(self, messages: List[ChatMessage]) -> str: ...
    def estimate_cost(self, input_chars: int, output_chars: int) -> float: ...


class ModelRegistry:
    """模型注册表：记录档案、兼容性测试与最近验证时间。"""

    def __init__(self) -> None:
        self._profiles: Dict[str, ModelProfile] = {}

    def register(self, profile: ModelProfile) -> None:
        self._profiles[profile.model_id] = profile

    def get(self, model_id: str) -> Optional[ModelProfile]:
        return self._profiles.get(model_id)

    def all(self) -> List[ModelProfile]:
        return list(self._profiles.values())

    def record_verification(self, model_id: str, passed: bool) -> Dict[str, Any]:
        p = self._profiles.get(model_id)
        if not p:
            raise KeyError(model_id)
        p.compat_tested = passed
        p.last_verified_at = time.time()
        return {"model_id": model_id, "compat_tested": passed,
                "verified_at": p.last_verified_at}

    def new_instance_id(self) -> str:
        return uuid.uuid4().hex[:10]
