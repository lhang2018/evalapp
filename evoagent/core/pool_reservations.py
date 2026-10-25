"""跨任务池（Provider/Expert 等）的原子共享预留（ALN-15-P）。

旧池（budget_ledger 的 scope:day/month 记账 + BudgetLedger.check 的
「先查后账」检查）在多任务并发时允许同一额度被重复合法消费。
本模块把 provider/expert 池转换成 ALN-15 预留账本上的原子共享账户：

- 每个池按周期建立**不可变**生命周期账户（kind=pool，专用命名空间
  `pool-v1:<scope>:<period>:<stamp>`；日期/月份切换即新账户，旧账户
  已花用量永久保留，符合 15-A 契约「账户不因周期重置」）。
- 每次模型调用前在同一 SQLite `BEGIN IMMEDIATE` 事务内持额
  （保守上界 = 单次调用费用上限 + 1 次调用），并发任务不允许双重占用。
- 无正数费用上界的池**拒绝原子化**并回退旧检查（与 15-R 的
  「未知价格无宿主正数上界时发送前拒绝」同语义；不假定零费）。
- 未下发即过期 → 退款；已下发未结算 → 未知（对账前持续占用额度），
  与 15-A recover_expired 状态机一致。

本服务只提供额度竞争，不是动作授权；不改旧池的记账与报表语义。
"""

from decimal import ROUND_CEILING, Decimal
import math
import time
import uuid

from .budget import BudgetExceeded

#: 允许原子化的池作用域前缀（provider/expert 旧池；其余保持旧检查）
POOL_PREFIXES = ("provider:", "expert:")

_MAX_UNITS = 2 ** 53 - 1


def pool_micros(value):
    """与 core.runtime_budget.micros 相同口径：1元=1,000,000 微币种。"""
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise BudgetExceeded("pricing", "费用上界必须是有限非负数")
    amount = int((Decimal(str(value)) * 1_000_000).to_integral_value(
        rounding=ROUND_CEILING))
    if amount > _MAX_UNITS:
        raise BudgetExceeded("pricing", "费用超出整数上限")
    return amount


def _scope(scope):
    if not isinstance(scope, str) or not any(
            scope.startswith(prefix) for prefix in POOL_PREFIXES):
        raise ValueError("pool scope must start with provider:/expert:")
    body = scope.split(":", 1)[1].strip()
    if not body or len(body) > 128:
        raise ValueError("pool scope body must be 1-128 characters")
    return scope


def _period(period):
    if period not in ("day", "month"):
        raise ValueError("period must be day or month")
    return period


def _stamp(period, at):
    format_string = "%Y-%m-%d" if period == "day" else "%Y-%m"
    return time.strftime(format_string, time.localtime(at))


def _positive_int(value, label):
    if type(value) is not int or not 0 < value <= _MAX_UNITS:
        raise ValueError(label + " requires a positive integer bound")
    return value


class PoolReservations:
    """旧池的原子预留服务；只提供额度，不是权限。"""

    AUDIT = {"actor": "budget:pool", "reason": "atomic shared pool hold"}

    def __init__(self, ledger, *, clock=None):
        if ledger.db is None:
            raise ValueError("pool reservations require a Database-backed ledger")
        self.ledger, self.db = ledger, ledger.db
        self.store = ledger.reservation_store(clock=clock)
        self.clock = clock or time.time

    def _now(self):
        return self.clock()

    # ---- 账户准备 ----

    @classmethod
    def account_name(cls, scope, period, at=None):
        return (f"pool-v1:{_scope(scope)}:{_period(period)}:"
                f"{_stamp(period, time.time() if at is None else at)}")

    def prepare(self, scope, *, max_model_calls, max_cost, period="day",
                actor=None, reason=None):
        """按周期建立不可变池账户；同参数重放返回原账户。

        缺任一正数上界即拒绝（不假定零费），调用方可回退旧检查。
        """
        name = self.account_name(scope, period, self._now())
        calls = _positive_int(int(max_model_calls), "max_model_calls")
        micros = pool_micros(max_cost)
        if micros <= 0:
            raise ValueError(
                "atomized pool requires a positive cost upper bound")
        limits = {"model_calls": calls, "cost_micros": micros}
        audit = self._audit(actor, reason)
        try:
            account = self.store.get_account(name)
        except KeyError:
            return self.store.create_account(
                name, limits, kind="pool",
                reference=f"budget-pool-v1|{scope}|{period}",
                currency="USD", parent_id=None, **audit)
        if account["limits"] != limits or account["kind"] != "pool" \
                or account["currency"] != "USD":
            raise ValueError(
                "atomized pool account identity and limits are immutable")
        return account

    def account_for(self, scope, period="day", *, at=None):
        """查询周期账户；未准备过则抛 KeyError。"""
        name = self.account_name(scope, period, at)
        return self.store.get_account(name)

    # ---- 原子持额 ----

    def hold(self, scope, *, cost_upper, calls_upper=1, period="day",
             ttl_seconds, actor=None, reason=None):
        """原子预留一次调用份容量；返回预留记录。

        持额前清扫全部过期预留（未下发→退款，已下发→转 unknown），
        保证崩溃窗口的额度不丢失；持额本身在一个 IMMEDIATE 事务内完成，
        并发任务对同一池的持额串行、不双重占用。
        """
        account = self.account_for(scope, period, at=self._now())
        amounts = {"model_calls": _positive_int(int(calls_upper), "calls_upper"),
                   "cost_micros": pool_micros(cost_upper)}
        audit = self._audit(actor, reason)
        self.sweep(**audit)
        return self.store.reserve(
            account["account_id"], f"pool-hold:v1|{uuid.uuid4().hex}", amounts,
            ttl_seconds=ttl_seconds, **audit)

    def get(self, reservation_id):
        return self.store.get(reservation_id)

    def sweep(self, *, actor=None, reason=None):
        return self.store.recover_expired(**self._audit(actor, reason))

    def settle(self, reservation_id, actual, *, evidence_ref):
        return self.store.settle(
            reservation_id, actual, evidence_ref=evidence_ref,
            **self.AUDIT)

    def mark_dispatched(self, reservation_id):
        """一次性领取：调用已真实发起；此后过期按未验证对账，不退款。"""
        return self.store.mark_dispatched(reservation_id, **self.AUDIT)

    def unknown(self, reservation_id, *, evidence_ref):
        return self.store.mark_unknown(
            reservation_id, evidence_ref=evidence_ref, **self.AUDIT)

    def refund(self, reservation_id):
        return self.store.refund(reservation_id, **self.AUDIT)

    def balance(self, scope, period="day", *, at=None):
        account = self.account_for(
            scope, period, at=self._now() if at is None else at)
        return self.store.balance(account["account_id"])

    def _audit(self, actor, reason):
        audit = dict(self.AUDIT)
        if actor:
            audit["actor"] = str(actor)
        if reason:
            audit["reason"] = str(reason)
        return audit
