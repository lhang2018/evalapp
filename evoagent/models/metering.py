"""模型调用计量包装（PRD 40 / Arch 20.1）。

只观测调用结果与时延；不改变适配器行为，也不把成功率当质量结论。
"""

import time
from typing import Any, Dict, List, Optional

from .base import Action, ChatMessage


class MeteredModel:
    def __init__(self, inner: Any, bus: Any = None,
                 component_id: str = "model_meter"):
        self.inner = inner
        self.bus = bus
        self.component_id = component_id

    def profile(self):
        return self.inner.profile()

    def estimate_cost(self, input_chars: int, output_chars: int) -> float:
        return self.inner.estimate_cost(input_chars, output_chars)

    def generate(self, messages: List[ChatMessage]) -> str:
        return self._observe("generate", lambda: self.inner.generate(messages))

    def suggest_action(self, messages: List[ChatMessage],
                       available_tools: List[Dict[str, Any]]) -> Action:
        return self._observe(
            "suggest_action",
            lambda: self.inner.suggest_action(messages, available_tools))

    def _observe(self, op: str, fn):
        start = time.time()
        ok = True
        error_class: Optional[str] = None
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            ok = False
            error_class = type(exc).__name__
            raise
        finally:
            if self.bus is not None:
                try:
                    model_id = self.inner.profile().model_id
                except Exception:  # noqa: BLE001
                    model_id = "unknown"
                try:
                    self.bus.emit(
                        event_type="model_call",
                        component_id=self.component_id,
                        severity="info" if ok else "error",
                        payload={"model_id": model_id, "op": op, "ok": ok,
                                 "error_class": error_class,
                                 "latency_s": round(time.time() - start, 4)})
                except Exception:  # noqa: BLE001 —— 观测失败不影响调用
                    pass
