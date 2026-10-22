"""数据隐私分级与上传策略（PRD 75/86 / Arch 18.5）。

服务端与安全回归套件共用同一判定，避免两处策略漂移。
"""

PRIVACY_LEVELS = ("standard", "sensitive", "secret", "private")
UPLOADABLE_LEVELS = frozenset({"standard"})

# 契约 §3 四值口径（ALN-16 统一跨端事件）：legacy 分类向此映射，
# 导出是否允许由等级与授权共同决定。
SPEC_PRIVACY_LEVELS = ("public", "internal", "sensitive", "restricted")
SPEC_EXPORTABLE_LEVELS = frozenset({"public", "internal"})
_SPEC_MAP = {"standard": "internal", "sensitive": "sensitive",
             "secret": "restricted", "private": "restricted"}


def is_uploadable(classification: str) -> bool:
    """只有 standard 级事件允许离开设备/进入服务端。"""
    return classification in UPLOADABLE_LEVELS


def to_spec_level(classification: str) -> str:
    """legacy 分类或四值等级统一为 public/internal/sensitive/restricted。"""
    if classification in SPEC_PRIVACY_LEVELS:
        return classification
    return _SPEC_MAP.get(classification, "restricted")


def is_spec_exportable(level: str, authorized: bool = False) -> bool:
    """导出同时要求等级可导出与显式授权；未知等级按 restricted 处理。"""
    return authorized and level in SPEC_EXPORTABLE_LEVELS
