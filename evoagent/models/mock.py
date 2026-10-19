"""MockModel：无 Key 完整跑通全链路（测试与演示基线）。

按意图使用 scripted 序列决策，行为确定、可测试、零成本。
约定：
- 引擎每轮把上一轮我方建议（assistant，JSON 格式）与工具结果（tool，"<tool>::<json>"）追加进 messages。
- refine 模式（失败反馈）：若最近一次工具结果是 error，则跳到恢复路径（finish 降级）。
"""

import json
import re
import time
from typing import Any, Dict, List

from .base import Action, ChatMessage, ModelProfile

SCRIPTS: Dict[str, List[Dict[str, Any]]] = {
    "time_query": [
        {"kind": "tool_call", "tool": "current_time"},
        {"kind": "finish", "answer": "当前时间："},
    ],
    "computation": [
        {"kind": "tool_call", "tool": "calculate"},
        {"kind": "finish", "answer": "计算结果："},
    ],
    "note_taking": [
        {"kind": "tool_call", "tool": "file_write"},
        {"kind": "finish", "answer": "笔记已写入（见工具结果）"},
    ],
    "weather": [
        {"kind": "tool_call", "tool": "http_get"},
        {"kind": "finish", "answer": "已查询天气（见工具结果）"},
    ],
    "general": [
        {"kind": "finish", "answer": "已按通用流程处理："},
    ],
}


class MockModel:
    def __init__(self, default_intent: str = "general"):
        self.default_intent = default_intent
        self.model_id = "mock-1"

    def profile(self) -> ModelProfile:
        return ModelProfile(model_id=self.model_id, provider="builtin",
                            context_limit=100000, supports_tool_call=True,
                            price_per_1k_input=0.0, price_per_1k_output=0.0,
                            compat_tested=True, last_verified_at=time.time())

    def generate(self, messages: List[ChatMessage]) -> str:
        for m in reversed(messages):
            if m.role == "assistant":
                return m.content
        return ""

    # ---- 核心 ----
    def suggest_action(self, messages: List[ChatMessage],
                       available_tools: List[Dict[str, Any]]) -> Action:
        intent = self._intent_of(messages)
        user_goal, user_kwargs = self._goal_of(messages)
        last_tool_ok, last_tool_result = self._last_tool_result(messages)
        step = self._step_count(messages)

        # 失败反馈：有限重试后降级（不盲目重复）
        if not last_tool_ok and step > 0:
            return Action(kind="finish",
                          answer=f"工具执行失败（{last_tool_result}），按恢复规则安全结束。",
                          reasoning="mock-recovery", confidence=0.5)

        script = SCRIPTS.get(intent, SCRIPTS["general"])
        if step >= len(script):
            return Action(kind="finish", answer="任务流程完成。", confidence=0.6,
                          reasoning="mock-fallback")

        d = script[step]
        if d["kind"] == "tool_call":
            args = self._args_for_tool(d["tool"], user_goal, user_kwargs)
            return Action(kind="tool_call", tool=d["tool"], args=args,
                          reasoning=f"__action__{d['tool']}", confidence=0.9)
        # finish：把工具结果拼接进答案
        answer = d["answer"] + (last_tool_result if last_tool_result else user_goal)
        return Action(kind="finish", answer=answer, reasoning="mock-finish", confidence=0.85)

    # ---- 解析 helpers ----
    def _intent_of(self, messages: List[ChatMessage]) -> str:
        for m in messages:
            if m.role == "system" and m.content.startswith("intent:"):
                raw = m.content.split(":", 1)[1]
                return raw.split("|", 1)[0].strip()
        return self.default_intent

    def _goal_of(self, messages: List[ChatMessage]):
        for m in messages:
            if m.role == "user" and m.content.startswith("goal:"):
                rest = m.content.split(":", 1)[1]
                return rest, self._extract_kwargs(rest)
        return "", {}

    def _extract_kwargs(self, text: str) -> Dict[str, str]:
        kw: Dict[str, str] = {}
        m = re.search(r"path\s*[=:：]\s*([^\s,，。;；]+)", text)
        if m:
            kw["path"] = m.group(1)
        m = re.search(r"content\s*[=:：]\s*([^\s,，]+)", text)
        if m:
            kw["content"] = m.group(1)
        expr = re.search(r"(-?[\d.]+(?:\s*[()+\-*/%.]\s*-?[\d.]+)+)", text)
        if expr:
            kw["expression"] = expr.group(1)
        m = re.search(r"url\s*[=:：]\s*(\S+)", text)
        if m:
            kw["url"] = m.group(1)
        return kw

    def _step_count(self, messages: List[ChatMessage]) -> int:
        n = 0
        for m in messages:
            if m.role == "assistant":
                n += 1
        return n

    def _last_tool_result(self, messages: List[ChatMessage]):
        for m in reversed(messages):
            if m.role == "tool":
                name, _, val = m.content.partition("::")
                try:
                    parsed = json.loads(val)
                except Exception:
                    parsed = {"value": val}
                if isinstance(parsed, dict) and parsed.get("ok") is False:
                    return False, val
                return True, _stringify(parsed)
        return True, ""

    def _args_for_tool(self, tool: str, goal: str, kw: Dict[str, str]) -> Dict[str, Any]:
        if tool == "calculate":
            return {"expression": kw.get("expression") or "1+1"}
        if tool == "file_write":
            return {"path": kw.get("path") or "notes/out.txt",
                    "content": kw.get("content") or goal}
        if tool == "http_get":
            return {"url": kw.get("url") or "https://mock.local/data"}
        return {}


def _stringify(parsed: Any) -> str:
    if isinstance(parsed, dict):
        if "data" in parsed and not isinstance(parsed["data"], (dict, list)):
            return str(parsed["data"])
        return json.dumps(parsed, ensure_ascii=False)
    return str(parsed)
