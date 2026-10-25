"""待核验结论与状态（ALN-28-A）。

一个 `Claim` 是「某对象在某时间范围内的事实断言」，带来源类型标记：
- `model_generated=True` 表示由模型自己生成（默认），**不能自证可信**；
- `valid_from/valid_until` 是适用时间窗，过期即 STALE；
- `derived_from_internal=True` 表示由内部已核验事实推导（可标 INFERRED，
  但仍不等于外部已核验）。
"""

import time
import math
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ClaimState(str, Enum):
    SUPPORTED = "supported"        # 有可访问外部来源直接支持
    CONTRADICTED = "contradicted"  # 来源之间冲突
    INFERRED = "inferred"          # 由内部已核验事实推导，非外部直接支持
    UNKNOWN = "unknown"            # 无外部证据（模型自述不判可信）
    STALE = "stale"                # 适用时间已过


@dataclass
class Claim:
    claim_id: str
    statement: str
    claim_key: str
    model_generated: bool = True
    valid_from: Optional[float] = None
    valid_until: Optional[float] = None
    applies_to: str = ""
    derived_from_internal: bool = False
    notes: str = ""

    def __post_init__(self):
        if any(not isinstance(v, str) or not v.strip() for v in (self.claim_id, self.statement, self.claim_key)):
            raise ValueError("结论身份、正文和匹配键不能为空")
        for value in (self.valid_from, self.valid_until):
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise ValueError("适用时间必须是有限时间戳")
        if self.valid_from is not None and self.valid_until is not None and self.valid_from > self.valid_until:
            raise ValueError("适用时间窗起点不能晚于终点")

    def is_expired(self, now: float) -> bool:
        return self.valid_until is not None and now > self.valid_until

    def applicable_window(self) -> Dict[str, Any]:
        return {"valid_from": self.valid_from,
                "valid_until": self.valid_until}

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Claim":
        fields = {name: data[name] for name in cls.__dataclass_fields__
                  if name in data}
        return cls(**fields)
