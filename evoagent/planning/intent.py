"""意图识别（PRD 7-8 / Architecture 4.2）。

显式 hint 优先；否则模型在已注册意图内做 JSON 分类；模型失败回退关键字启发式。
低置信度结果必须被标记，由控制器按风险决定是否暂停澄清（PRD 7.4）。
"""

from typing import Iterable, List, Optional, Tuple

from ..models.base import ChatMessage
from .planner import _parse_json

_KEYWORDS: List[Tuple[str, Tuple[str, ...]]] = [
    ("time_query", ("几点", "时间", "日期", "星期", "time", "date")),
    ("computation", ("计算", "算出", "等于", "加", "减", "乘", "除", "calculate")),
    ("note_taking", ("笔记", "记录", "备忘", "note", "todo")),
    ("weather", ("天气", "气温", "下雨", "weather")),
    ("comic_drama", ("漫剧", "漫画", "分镜", "配音", "comic", "drama")),
    ("programming", ("代码", "编程", "调试", "报错", "python", "bug", "code")),
    ("mobile_operation", ("手机", "应用", "打开", "点击", "app", "android")),
]


class IntentClassifier:
    def __init__(self, model_adapter=None,
                 known_intents: Optional[Iterable[str]] = None,
                 threshold: float = 0.5):
        self.model = model_adapter
        self.known = list(known_intents or []) or ["general"]
        self.threshold = threshold

    def classify(self, goal: str, *, hint: Optional[str] = None,
                 domain_hint: Optional[str] = None
                 ) -> Tuple[str, str, float, str]:
        """返回 (intent, domain, confidence, method)。"""
        if hint:
            return hint, domain_hint or "", 1.0, "hint"
        if self.model is not None:
            try:
                intent, domain, conf = self._model_classify(goal, domain_hint)
                if intent in self.known:
                    return intent, domain or domain_hint or "", conf, "model"
            except Exception:  # noqa: BLE001 —— 分类失败不阻断任务
                pass
            fallback_method = "heuristic_fallback"
        else:
            fallback_method = "heuristic"
        intent = self._heuristic(goal)
        conf = 0.4 if intent == "general" else 0.6
        return intent, domain_hint or "", conf, fallback_method

    # ---- 模型分类 ----
    def _model_classify(self, goal: str, domain_hint: Optional[str]
                        ) -> Tuple[str, str, float]:
        options = ", ".join(self.known)
        system = ChatMessage(role="system", content=(
            "你是意图分类器。只输出 JSON："
            '{"intent":"...","domain":"...","confidence":0.0}。'
            f"intent 必须属于: {options}。confidence 是 0-1 的小数。"))
        user = ChatMessage(role="user",
                           content=f"goal:{goal}\ndomain_hint:{domain_hint or ''}")
        data = _parse_json(self.model.generate([system, user]))
        intent = str(data.get("intent") or "").strip()
        domain = str(data.get("domain") or "").strip()
        try:
            conf = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = round(min(1.0, max(0.0, conf)), 2)
        return intent, domain, conf

    # ---- 启发式兜底 ----
    @staticmethod
    def _heuristic(goal: str) -> str:
        text = (goal or "").lower()
        for intent, words in _KEYWORDS:
            if any(w in text for w in words):
                return intent
        return "general"
