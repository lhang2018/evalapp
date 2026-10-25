"""供应商价格档案与成本解析（ALN-08-P）。

补的是「价格从哪来、什么时候有效、未知时算什么」这三件事，不改动已接受的
`models/metering.py` 计量口径：

- **价格有来源/有效期**：每条价格必须有 `source_ref`（供应商价目/账单编号）与
  生效区间；同一模型同一币种的有效区间不允许重叠（避免二义性）。
- **未知只计数不造金额**：价格缺失、未生效、已过期、币种不符时 `cost` 返回
  ``None`` 并给出原因码；聚合只累加已定价（含显式免费）的金额，未知调用只进
  ``unknown_count``，绝不按 0 元或估算价代替。
- **后续对账**：同一批用量在价格档案补齐/生效后可重新解析，得到金额与来源，
  供账单对账（``models/billing.py``）解释差异。

价格单位与既有 `ModelProfile.price_per_1k_*` 一致：每 1K tokens 计价。
"""

import json
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

STATUS_PRICED = "priced"
STATUS_FREE = "free"
UNKNOWN_NO_PRICE = "unknown:no_price"
UNKNOWN_NOT_EFFECTIVE = "unknown:not_yet_effective"
UNKNOWN_EXPIRED = "unknown:expired"
UNKNOWN_INVALID = "unknown:invalid_usage"

UNKNOWN_STATUSES = (UNKNOWN_NO_PRICE, UNKNOWN_NOT_EFFECTIVE, UNKNOWN_EXPIRED,
                    UNKNOWN_INVALID)


class PriceError(ValueError):
    """价格档案记录非法（缺来源、区间非法、重叠）。"""


@dataclass(frozen=True)
class PriceEntry:
    price_id: str
    model_id: str
    input_per_1k: float
    output_per_1k: float
    currency: str = "USD"
    source_ref: str = ""            # 供应商价目 URL / 账单编号：必须有来源
    effective_from: float = 0.0
    effective_to: Optional[float] = None
    note: str = ""

    def validate(self) -> "PriceEntry":
        if not isinstance(self.price_id, str) or not self.price_id.strip():
            raise PriceError("price_id 不得为空")
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise PriceError("model_id 不得为空")
        if not isinstance(self.source_ref, str) or not self.source_ref.strip():
            raise PriceError("价格必须有来源（供应商价目/账单编号）")
        if not isinstance(self.currency, str) or len(self.currency) != 3:
            raise PriceError("currency 必须是 3 位币种代码")
        for name, value in (("input_per_1k", self.input_per_1k),
                            ("output_per_1k", self.output_per_1k)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise PriceError(name + " 必须是数字")
            if not math.isfinite(float(value)) or value < 0:
                raise PriceError(name + " 必须是非负有限数")
        if not math.isfinite(float(self.effective_from)) or self.effective_from < 0:
            raise PriceError("effective_from 非法")
        if self.effective_to is not None and (
                not math.isfinite(float(self.effective_to))
                or float(self.effective_to) <= float(self.effective_from)):
            raise PriceError("effective_to 必须晚于 effective_from")
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {"price_id": self.price_id, "model_id": self.model_id,
                "input_per_1k": float(self.input_per_1k),
                "output_per_1k": float(self.output_per_1k),
                "currency": self.currency, "source_ref": self.source_ref,
                "effective_from": float(self.effective_from),
                "effective_to": None if self.effective_to is None
                else float(self.effective_to), "note": self.note}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PriceEntry":
        fields = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in fields}).validate()

    def covers(self, at: float) -> bool:
        return (float(self.effective_from) <= at
                and (self.effective_to is None or at < float(self.effective_to)))


class PriceCatalog:
    """版本化价格档案（SQLite 持久；重启后同一批用量可重新解析）。"""

    def __init__(self, db: Any, *, clock=None):
        self.db = db
        self.clock = clock or time.time
        conn = getattr(db, "_conn", None)
        if conn is None:
            raise PriceError("PriceCatalog 需要带连接的 Database")

    # ---- 登记 ----
    def register(self, entry: PriceEntry) -> PriceEntry:
        entry.validate()
        with self.db._lock:
            existing = self.db._conn.execute(
                "SELECT data FROM model_prices WHERE price_id=?",
                (entry.price_id,)).fetchone()
            if existing is not None:
                stored = PriceEntry.from_dict(json.loads(existing["data"]))
                if stored.to_dict() != entry.to_dict():
                    raise PriceError("price_id 已存在且内容不同（价格档案不可改）")
                return stored
            for other in self.entries(model_id=entry.model_id,
                                      currency=entry.currency):
                if other.price_id == entry.price_id:
                    continue
                if self._overlap(other, entry):
                    raise PriceError(
                        "同一模型同币种的有效区间重叠: %s vs %s"
                        % (other.price_id, entry.price_id))
            self.db._conn.execute(
                "INSERT INTO model_prices VALUES (?,?,?,?,?,?,?)",
                (entry.price_id, entry.model_id, entry.currency,
                 entry.source_ref, float(entry.effective_from),
                 None if entry.effective_to is None else float(entry.effective_to),
                 json.dumps(entry.to_dict(), ensure_ascii=False, sort_keys=True)))
            self.db._conn.commit()
        return entry

    @staticmethod
    def _overlap(left: PriceEntry, right: PriceEntry) -> bool:
        left_end = float("inf") if left.effective_to is None else float(left.effective_to)
        right_end = float("inf") if right.effective_to is None else float(right.effective_to)
        return (float(left.effective_from) < right_end
                and float(right.effective_from) < left_end)

    def entries(self, *, model_id: Optional[str] = None,
                currency: Optional[str] = None) -> List[PriceEntry]:
        sql, args = "SELECT data FROM model_prices WHERE 1=1", []
        if model_id is not None:
            sql += " AND model_id=?"
            args.append(model_id)
        if currency is not None:
            sql += " AND currency=?"
            args.append(currency)
        sql += " ORDER BY model_id, effective_from"
        rows = self.db._conn.execute(sql, args).fetchall()
        return [PriceEntry.from_dict(json.loads(row["data"])) for row in rows]

    # ---- 解析 ----
    def price_for(self, model_id: str, at: Optional[float] = None,
                  currency: str = "USD") -> Tuple[Optional[PriceEntry], str]:
        moment = self.clock() if at is None else float(at)
        candidates = self.entries(model_id=model_id, currency=currency)
        if not candidates:
            return None, UNKNOWN_NO_PRICE
        for entry in candidates:
            if entry.covers(moment):
                return entry, (STATUS_FREE if (entry.input_per_1k == 0
                                               and entry.output_per_1k == 0)
                               else STATUS_PRICED)
        if all(moment < entry.effective_from for entry in candidates):
            return None, UNKNOWN_NOT_EFFECTIVE
        return None, UNKNOWN_EXPIRED

    def cost(self, model_id: str, input_tokens: Optional[int],
             output_tokens: Optional[int], *, at: Optional[float] = None,
             currency: str = "USD") -> Dict[str, Any]:
        """单次调用成本解析；未知返回 None 原因码，绝不造金额。"""
        entry, status = self.price_for(model_id, at=at, currency=currency)
        base = {"model_id": model_id, "currency": currency,
                "price_id": entry.price_id if entry else None,
                "source_ref": entry.source_ref if entry else None}
        if status in UNKNOWN_STATUSES:
            return {**base, "cost": None, "status": status}
        if status == STATUS_FREE:
            return {**base, "cost": 0.0, "status": STATUS_FREE}
        tokens_in, tokens_out = self._tokens(input_tokens, output_tokens)
        if tokens_in is None or tokens_out is None:
            return {**base, "cost": None, "status": UNKNOWN_INVALID}
        amount = (tokens_in * float(entry.input_per_1k)
                  + tokens_out * float(entry.output_per_1k)) / 1000.0
        if not math.isfinite(amount) or amount < 0:
            return {**base, "cost": None, "status": UNKNOWN_INVALID}
        return {**base, "cost": round(amount, 6), "status": STATUS_PRICED,
                "input_tokens": tokens_in, "output_tokens": tokens_out}

    @staticmethod
    def _tokens(input_tokens: Any, output_tokens: Any):
        values = []
        for value in (input_tokens, output_tokens):
            if isinstance(value, bool) or value is None:
                return None, None
            try:
                number = int(value)
            except (TypeError, ValueError, OverflowError):
                return None, None
            if number < 0:
                return None, None
            values.append(number)
        return values[0], values[1]

    def aggregate(self, rows: Iterable[Dict[str, Any]], *,
                  at: Optional[float] = None, currency: str = "USD") -> Dict[str, Any]:
        """聚合一批用量：金额只来自已定价/免费行，未知行只计数。"""
        amount = 0.0
        unknown = free = priced = 0
        by_status: Dict[str, int] = {}
        rows_out: List[Dict[str, Any]] = []
        for row in rows:
            resolved = self.cost(row.get("model_id", ""),
                                 row.get("input_tokens"),
                                 row.get("output_tokens"),
                                 at=at, currency=row.get("currency", currency))
            by_status[resolved["status"]] = by_status.get(resolved["status"], 0) + 1
            if resolved["status"] == STATUS_PRICED:
                priced += 1
                amount += float(resolved["cost"])
            elif resolved["status"] == STATUS_FREE:
                free += 1
            else:
                unknown += 1
            rows_out.append({**resolved, "request_ref": row.get("request_ref")})
        return {"amount": round(amount, 6), "priced_count": priced,
                "free_count": free, "unknown_count": unknown,
                "unknown_amount": None,   # 未知费用没有金额，不填 0
                "by_status": by_status, "currency": currency,
                "rows": rows_out,
                "scope": "usage_rows_only"}
