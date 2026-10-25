"""计量与供应商账单对账（ALN-08-P：费用对账）。

验收口径：
- **差异可解释**：每一行计量/账单差异都归入固定原因码（``matched`` /
  ``rounding`` / ``amount_mismatch`` / ``currency_mismatch`` /
  ``invoice_missing_line`` / ``bill_only_line`` / ``unknown_price``），
  不允许出现「无原因差异」；
- **未知只计数不造金额**：计量行金额为 ``None``（价格未知）时只进
  ``unknown_count`` 并标记 ``unknown_price``，不会把 0 或估算价当实际费用；
- **放行条件**：只有 ``matched`` 与容差内 ``rounding`` 才算对账通过；
  其余原因全部进 ``followups``（后续对账项），``passed=False``。

本模块是纯函数，不写库、不改计量口径；输入来自既有
``models/metering.py`` 的 ``ModelUsage``（金额由 ``models/pricing.py`` 解析）。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

ROUNDING_TOLERANCE = 0.01

REASON_MATCHED = "matched"
REASON_ROUNDING = "rounding"
REASON_AMOUNT_MISMATCH = "amount_mismatch"
REASON_CURRENCY_MISMATCH = "currency_mismatch"
REASON_INVOICE_MISSING = "invoice_missing_line"
REASON_BILL_ONLY = "bill_only_line"
REASON_UNKNOWN_PRICE = "unknown_price"
REASON_DUPLICATE_BILL = "duplicate_bill_line"

#: 需要后续对账（不能算「已解释且无需处理」）的原因
FOLLOWUP_REASONS = frozenset({
    REASON_AMOUNT_MISMATCH, REASON_CURRENCY_MISMATCH,
    REASON_INVOICE_MISSING, REASON_BILL_ONLY,
    REASON_UNKNOWN_PRICE, REASON_DUPLICATE_BILL,
})


@dataclass(frozen=True)
class MeteringLine:
    """内部计量行；``amount=None`` 表示价格未知（不折算金额）。"""

    request_ref: str
    model_id: str
    amount: Optional[float] = None
    currency: str = "USD"
    status: str = ""


@dataclass(frozen=True)
class BillLine:
    """供应商账单行。"""

    request_ref: str
    model_id: str
    amount: float
    currency: str = "USD"
    invoice_ref: str = ""


@dataclass
class Reconciliation:
    period: str = ""
    currency: str = "USD"
    tolerance: float = ROUNDING_TOLERANCE
    rows: List[Dict[str, Any]] = field(default_factory=list)
    followups: List[Dict[str, Any]] = field(default_factory=list)
    unknown_count: int = 0
    billed_total: float = 0.0
    metered_known_total: float = 0.0
    delta: float = 0.0
    passed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        for row in self.rows:
            counts[row["reason"]] = counts.get(row["reason"], 0) + 1
        return {"period": self.period, "currency": self.currency,
                "tolerance": self.tolerance, "rows": self.rows,
                "followups": self.followups,
                "explanations": counts,
                "unknown_count": self.unknown_count,
                "unknown_amount": None,     # 未知费用不填金额
                "billed_total": round(self.billed_total, 6),
                "metered_known_total": round(self.metered_known_total, 6),
                "delta": round(self.delta, 6),
                "passed": self.passed,
                "scope": "metering_vs_invoice"}


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def reconcile(metering: Iterable[MeteringLine], bill: Iterable[BillLine],
              *, period: str = "", currency: str = "USD",
              tolerance: float = ROUNDING_TOLERANCE) -> Reconciliation:
    """按 ``request_ref`` 对账：返回逐行原因、后续项与是否通过。"""
    result = Reconciliation(period=period, currency=currency,
                            tolerance=float(tolerance))
    bill_rows = list(bill)
    indexed: Dict[str, List[BillLine]] = {}
    for line in bill_rows:
        indexed.setdefault(line.request_ref, []).append(line)
    matched_refs: set = set()
    unknown_refs: set = set()   # 内部金额未知：账单行存在也不参与金额比较
    for line in metering:
        amount = _number(line.amount)
        rows = indexed.get(line.request_ref, [])
        if amount is None:
            result.unknown_count += 1
            matched_refs.add(line.request_ref)
            unknown_refs.add(line.request_ref)
            result.rows.append({"request_ref": line.request_ref,
                                "model_id": line.model_id,
                                "reason": REASON_UNKNOWN_PRICE,
                                "metered_amount": None,
                                "billed_amount": None, "delta": None,
                                "currency": line.currency})
            result.followups.append({"request_ref": line.request_ref,
                                     "reason": REASON_UNKNOWN_PRICE})
            continue
        result.metered_known_total += amount
        if not rows:
            result.rows.append({"request_ref": line.request_ref,
                                "model_id": line.model_id,
                                "reason": REASON_INVOICE_MISSING,
                                "metered_amount": round(amount, 6),
                                "billed_amount": None, "delta": None,
                                "currency": line.currency})
            result.followups.append({"request_ref": line.request_ref,
                                     "reason": REASON_INVOICE_MISSING})
            continue
        if len(rows) > 1:
            result.rows.append({"request_ref": line.request_ref,
                                "model_id": line.model_id,
                                "reason": REASON_DUPLICATE_BILL,
                                "metered_amount": round(amount, 6),
                                "billed_amount": round(sum(_number(row.amount) or 0.0
                                                           for row in rows), 6),
                                "delta": None, "currency": line.currency})
            result.followups.append({"request_ref": line.request_ref,
                                     "reason": REASON_DUPLICATE_BILL})
            matched_refs.add(line.request_ref)
            continue
        row = rows[0]
        matched_refs.add(line.request_ref)
        billed = _number(row.amount)
        if line.currency != row.currency or billed is None:
            result.rows.append({"request_ref": line.request_ref,
                                "model_id": line.model_id,
                                "reason": REASON_CURRENCY_MISMATCH
                                if line.currency != row.currency
                                else REASON_AMOUNT_MISMATCH,
                                "metered_amount": round(amount, 6),
                                "billed_amount": None if billed is None
                                else round(billed, 6),
                                "delta": None, "currency": line.currency})
            result.followups.append({
                "request_ref": line.request_ref,
                "reason": REASON_CURRENCY_MISMATCH
                if line.currency != row.currency else REASON_AMOUNT_MISMATCH})
            continue
        delta = round(billed - amount, 6)
        if abs(delta) <= tolerance:
            reason = REASON_MATCHED if abs(delta) < 1e-9 else REASON_ROUNDING
        else:
            reason = REASON_AMOUNT_MISMATCH
        result.rows.append({"request_ref": line.request_ref,
                            "model_id": line.model_id, "reason": reason,
                            "metered_amount": round(amount, 6),
                            "billed_amount": round(billed, 6),
                            "delta": delta, "currency": line.currency})
        if reason != REASON_MATCHED:
            if abs(delta) > tolerance:
                result.followups.append({"request_ref": line.request_ref,
                                         "reason": reason})
    for line in bill_rows:
        if line.request_ref in matched_refs:
            continue
        billed = _number(line.amount)
        if billed is not None:
            result.billed_total += billed
        result.rows.append({"request_ref": line.request_ref,
                            "model_id": line.model_id,
                            "reason": REASON_BILL_ONLY,
                            "metered_amount": None,
                            "billed_amount": None if billed is None
                            else round(billed, 6),
                            "delta": None, "currency": line.currency})
        result.followups.append({"request_ref": line.request_ref,
                                 "reason": REASON_BILL_ONLY})
    for line in bill_rows:
        billed = _number(line.amount)
        if (billed is not None and line.request_ref in matched_refs
                and line.request_ref not in unknown_refs):
            result.billed_total += billed
    result.delta = result.billed_total - result.metered_known_total
    result.passed = not result.followups
    return result
