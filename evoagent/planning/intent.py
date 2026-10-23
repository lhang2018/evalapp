"""意图识别（PRD 7-8 / Architecture 4.2）。

显式 hint 优先；否则模型在已注册意图内做 JSON 分类；模型失败回退关键字启发式。
低置信度结果必须被标记，由控制器按风险决定是否暂停澄清（PRD 7.4）。
"""

from typing import Iterable, List, Optional, Tuple

from ..models.base import ChatMessage
from ..core.budget import BudgetExceeded
from ..core.leases import LeaseLost
from .planner import _parse_json

_DESCRIPTIONS = {
    "general": "通用问答/闲聊/无法归入其他领域",
    "time_query": "查询当前时间、日期或星期",
    "computation": "数学计算与数值运算",
    "coding": "代码生成、调试、测试与依赖分析",
    "travel": "行程规划、交通与目的地信息",
    "weather": "天气查询",
    "comic_drama": "AI 漫剧：剧本、分镜、配音与合成",
    "mobile_operation": "Android 设备与 App 操作",
    "note_taking": "写笔记、记录待办",
}

_KEYWORDS: List[Tuple[str, Tuple[str, ...]]] = [
    ("time_query", ("几点", "时间", "日期", "星期", "time", "date")),
    ("computation", ("计算", "算出", "等于", "加", "减", "乘", "除", "calculate")),
    ("note_taking", ("笔记", "记录", "备忘", "note", "todo")),
    ("weather", ("天气", "气温", "下雨", "weather")),
    ("comic_drama", ("漫剧", "漫画", "分镜", "配音", "comic", "drama")),
    # 意图名必须与 _DESCRIPTIONS / 已注册专家一致。
    # 原写作 "programming" —— 该意图**从未注册**（描述表与专家表都用 "coding"），
    # 导致无 hint 的代码类任务全部落到 general 专家。F-53 路由评测查出此缺陷。
    ("coding", ("代码", "编程", "调试", "报错", "python", "bug", "code")),
    ("travel", ("行程", "旅游", "旅行", "出差", "路线", "攻略", "签证", "机票", "酒店")),
    ("mobile_operation", ("手机", "应用", "打开", "点击", "app", "android")),
]

#: 关键词表与描述表的一致性自检 —— 两者一旦漂移就会出现"幽灵意图"
#: （如曾经的 "programming"）或"永远无法命中的意图"（如曾经的 travel）。
#: 由 tests/test_routing_eval.py 锁定。
assert {intent for intent, _ in _KEYWORDS} <= set(_DESCRIPTIONS), (
    "关键词表含未注册意图（幽灵意图），会导致该意图无法路由到对应专家："
    f"{sorted({i for i, _ in _KEYWORDS} - set(_DESCRIPTIONS))}")


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
            except (BudgetExceeded, LeaseLost):
                raise
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
        options = "\n".join(
            f"- {name}: {_DESCRIPTIONS.get(name, name)}" for name in self.known)
        system = ChatMessage(role="system", content=(
            "你是意图分类器。只输出 JSON："
            '{"intent":"...","domain":"...","confidence":0.0}。'
            "优先选择具体领域；只有确实不属于任何具体领域时才选 general。\n"
            f"intent 必须属于以下之一:\n{options}\n"
            "confidence 是 0-1 的小数。"))
        user = ChatMessage(role="user",
                           content=f"goal:{goal}\ndomain_hint:{domain_hint or ''}")
        from ..core.runtime_budget import CLASSIFICATION_BUDGET, model_call
        data = _parse_json(model_call(CLASSIFICATION_BUDGET.get(), self.model,
                                     "generate", [system, user], phase="classification"))
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
