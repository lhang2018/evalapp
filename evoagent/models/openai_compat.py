"""OpenAI 兼容适配器：真实 LLM 接入（OpenAI / DeepSeek / Moonshot / vLLM 等）。

- Function Calling -> Action（建议）；执行权在运行时控制器（PRD 12.4）。
- 有限重试 + 退避（PRD 57）。
"""

import json
import time
from typing import Any, Dict, List

from ..tools.contract import ToolContract, ToolResult
from .base import Action, ChatMessage, ModelProfile, ModelRegistry

_MAX_RETRIES = 3
_BACKOFF_SECONDS = [1, 2, 4]


class OpenAICompatAdapter:
    """provider 配置来自 config.ModelProviderConfig；与 OpenAI Chat Completions 兼容。"""

    def __init__(self, model_id: str, api_key: str,
                 base_url: str = "https://api.openai.com/v1",
                 timeout_seconds: int = 60, registry: ModelRegistry | None = None):
        self.model_id = model_id
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds
        self._profile = ModelProfile(
            model_id=model_id, provider="openai-compat",
        )

    def profile(self) -> ModelProfile:
        return self._profile

    def generate(self, messages: List[ChatMessage]) -> str:
        payload = {"model": self.model_id,
                   "messages": [{"role": m.role, "content": m.content} for m in messages]}
        return self._chat(payload)["content"]

    def suggest_action(self, messages: List[ChatMessage],
                       available_tools: List[ToolContract]) -> Action:
        tools_payload = [_openai_tool(t) for t in available_tools]
        payload: Dict[str, Any] = {
            "model": self.model_id,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": 0.2,
        }
        if tools_payload:
            payload["tools"] = tools_payload
        resp = self._chat(payload)
        content = resp.get("content") or ""
        function_call = resp.get("function_call")
        tool_calls = resp.get("tool_calls")
        if tool_calls:
            call = tool_calls[0]
            fn = call.get("function", {})
            name = fn.get("name")
            args_raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_raw)
            except json.JSONDecodeError:
                args = {}
            return Action(kind="tool_call", tool=name, args=args,
                          reasoning=content or "model-tool-call", confidence=0.85)
        if function_call:
            name = function_call.get("name")
            try:
                args = json.loads(function_call.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            return Action(kind="tool_call", tool=name, args=args,
                          reasoning=content or "model-tool-call", confidence=0.85)
        # 无工具调用 → 视为给出答案
        if content:
            return Action(kind="finish", answer=content, reasoning="model-answer",
                          confidence=0.8)
        # 模型既无答案也无调用：保守要求澄清（PRD 8.3）
        return Action(kind="request_user",
                      answer="模型未给出明确行动，需要用户澄清。",
                      reasoning="model-empty", confidence=0.3)

    def estimate_cost(self, input_chars: int, output_chars: int) -> float:
        p = self._profile
        return (input_chars * p.price_per_1k_input + output_chars * p.price_per_1k_output) / 1000

    # ---- HTTP ----
    def _chat(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        import requests  # 本地环境已有；缺失时给出明确错误
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        last_err: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                r = requests.post(url, json=payload, headers=headers,
                                  timeout=self.timeout)
                if r.status_code == 200:
                    data = r.json()
                    msg = (data.get("choices") or [{}])[0].get("message", {})
                    out: Dict[str, Any] = {"content": msg.get("content")}
                    if msg.get("function_call"):
                        out["function_call"] = msg["function_call"]
                    if msg.get("tool_calls"):
                        out["tool_calls"] = msg["tool_calls"]
                    return out
                if r.status_code in (429, 500, 502, 503):
                    last_err = RuntimeError(f"HTTP {r.status_code}")
                else:
                    raise RuntimeError(f"LLM 调用失败 HTTP {r.status_code}: {r.text[:300]}")
            except requests.RequestException as e:
                last_err = e
            time.sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])
        raise RuntimeError(f"LLM 调用重试 {_MAX_RETRIES} 次仍失败: {last_err}")


def _openai_tool(t: ToolContract) -> Dict[str, Any]:
    """ToolContract -> OpenAI function 描述。"""
    return {
        "type": "function",
        "function": {
            "name": t.tool_id,
            "description": t.description,
            "parameters": t.input_schema,
        },
    }
