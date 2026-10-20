"""数据隐私分级与上传策略（PRD 75/86 / Arch 18.5）。

服务端与安全回归套件共用同一判定，避免两处策略漂移。
"""

PRIVACY_LEVELS = ("standard", "sensitive", "secret", "private")
UPLOADABLE_LEVELS = frozenset({"standard"})


def is_uploadable(classification: str) -> bool:
    """只有 standard 级事件允许离开设备/进入服务端。"""
    return classification in UPLOADABLE_LEVELS
