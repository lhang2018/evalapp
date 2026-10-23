"""Durable aggregate capacity, separate from per-task authorization (ALN-15).

Amounts are integer units; cost_micros is 1e-6 of the account's named currency.
Accounts are immutable lifetime allocations. Day/month policies can create a
new explicitly named account per period; expiry never resets spent capacity.
Legacy usage is mirrored in the SAME transaction on terminal reconciliation.
This internal service does not wire existing model/tool/Goal callers by itself.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import re
import time
import uuid

from .budget import BudgetExceeded
from .leases import assert_connection_lease

DIMENSIONS = frozenset({"cost_micros", "model_calls", "tool_calls", "tokens"})
MAX_UNITS = 2**53 - 1


class ReservationConflict(ValueError):
    """A replay conflicts with the immutable request or durable action state."""


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(label + " must be a nonempty reference (max 512 characters)")
    return value.strip()


def _units(value):
    if not isinstance(value, dict) or not value or set(value) - DIMENSIONS:
        raise ValueError("amounts require known nonempty resource dimensions")
    for amount in value.values():
        if type(amount) is not int or not 0 <= amount <= MAX_UNITS:
            raise ValueError("units must be nonnegative bounded integers")
    return dict(value)


def _duration(value):
    if type(value) not in (int, float) or value <= 0:
        raise ValueError("lease duration must be positive and finite")
    try:
        duration = float(value)
    except OverflowError as exc:
        raise ValueError("lease duration is too large") from exc
    if not math.isfinite(duration):
        raise ValueError("lease duration must be positive and finite")
    return duration


class BudgetReservations:
    def __init__(self, ledger, *, clock=None):
        if ledger.db is None:
            raise ValueError("reservations require a Database-backed BudgetLedger")
        self.ledger, self.db = ledger, ledger.db
        self.clock = clock or time.time

    def _now(self):
        value = self.clock()
        if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
            raise ValueError("clock must return a finite nonnegative timestamp")
        return float(value)

    @contextmanager
    def _write(self):
        with self.db._lock:
            # Never commit or roll back the caller's unrelated transaction.
            if self.db._conn.in_transaction:
                raise RuntimeError("reservation service cannot nest a transaction")
            self.db._conn.execute("BEGIN IMMEDIATE")
            try:
                assert_connection_lease(self.db._conn, self.db)
                yield
                self.db._conn.commit()
            except BaseException:
                self.db._conn.rollback()
                raise

    def _account(self, account_id):
        row = self.db._conn.execute("SELECT * FROM budget_accounts WHERE account_id=?",
                                    (account_id,)).fetchone()
        if row is None:
            raise KeyError("unknown budget account: " + str(account_id))
        result = dict(row)
        result["limits"] = json.loads(result["limits"])
        return result

    def _lineage(self, account_id):
        result, seen = [], set()
        while account_id is not None:
            if account_id in seen or len(seen) >= 32:
                raise ReservationConflict("budget account ancestry is cyclic or too deep")
            seen.add(account_id)
            account = self._account(account_id)
            result.append(account)
            account_id = account["parent_id"]
        return result

    def create_account(self, account_id, limits, *, kind, reference, currency,
                       actor, reason, parent_id=None):
        account_id = _text(account_id, "account_id")
        reference = _text(reference, "reference")
        actor, reason = _text(actor, "actor"), _text(reason, "reason")
        limits = _units(limits)
        if not isinstance(kind, str) or kind not in {"goal", "task", "experiment", "pool"}:
            raise ValueError("unknown budget account kind")
        if not isinstance(currency, str) or not re.fullmatch("[A-Z]{3}", currency):
            raise ValueError("currency requires an explicit three-letter code")
        if parent_id is not None:
            parent_id = _text(parent_id, "parent_id")
        with self._write():
            old = self.db._conn.execute("SELECT account_id FROM budget_accounts WHERE account_id=?",
                                         (account_id,)).fetchone()
            fields = dict(account_id=account_id, limits=limits, kind=kind,
                          reference=reference, currency=currency, parent_id=parent_id)
            if old:
                existing = self._account(account_id)
                if any(existing[key] != value for key, value in fields.items()):
                    raise ReservationConflict("account identity and limits are immutable")
                return existing
            if parent_id is not None:
                parents = self._lineage(parent_id)
                if len(parents) >= 32:
                    raise ValueError("account ancestry exceeds 32 levels")
                for parent in parents:
                    if (currency != parent["currency"] or set(limits) != set(parent["limits"])
                            or any(limits[key] > parent["limits"][key] for key in limits)):
                        raise ValueError("child account must retain and narrow parent units and currency")
            now = self._now()
            self.db._conn.execute("INSERT INTO budget_accounts VALUES (?,?,?,?,?,?,?,?,?)",
                (account_id, parent_id, kind, reference, currency, _json(limits), now, actor, reason))
            return self._account(account_id)

    def get_account(self, account_id):
        with self.db._lock:
            return self._account(account_id)

    def _get(self, reservation_id):
        row = self.db._conn.execute("SELECT data FROM budget_reservations WHERE reservation_id=?",
                                    (reservation_id,)).fetchone()
        if row is None:
            raise KeyError("unknown reservation: " + str(reservation_id))
        return json.loads(row[0])

    def get(self, reservation_id):
        with self.db._lock:
            return self._get(reservation_id)

    def _balance(self, account, now):
        held = dict.fromkeys(account["limits"], 0)
        spent = dict(held)
        unknown_count = unresolved_count = 0
        rows = self.db._conn.execute("SELECT r.data FROM budget_reservations r "
            "JOIN budget_reservation_accounts a USING(reservation_id) WHERE a.account_id=?",
            (account["account_id"],)).fetchall()
        for row in rows:
            reservation = json.loads(row[0])
            state = reservation["state"]
            if state in {"reserved", "unknown"}:
                for dimension, amount in reservation["amounts"].items():
                    held[dimension] += amount
            elif state == "settled":
                for dimension, amount in reservation["actual"].items():
                    spent[dimension] += amount
            unknown_count += state == "unknown"
            unresolved_count += (state == "unknown" or (state == "reserved" and
                reservation["dispatched"] and reservation["expires_at"] <= now))
        return dict(account_id=account["account_id"], currency=account["currency"],
            limits=account["limits"], held=held, spent=spent,
            available={k: account["limits"][k] - held[k] - spent[k] for k in held},
            unknown_count=unknown_count, reconciliation_required=bool(unresolved_count))

    def balance(self, account_id):
        with self.db._lock:
            return self._balance(self._account(account_id), self._now())

    def _save(self, reservation, event, actor, reason):
        reservation["revision"] += 1
        reservation["updated_at"] = self._now()
        self.db._conn.execute("UPDATE budget_reservations SET state=?,dispatched=?,data=? "
            "WHERE reservation_id=?", (reservation["state"], int(reservation["dispatched"]),
            _json(reservation), reservation["reservation_id"]))
        self.db._conn.execute("INSERT INTO budget_reservation_events VALUES (?,?,?,?,?,?,?)",
            (reservation["reservation_id"], reservation["revision"], event, actor, reason,
             reservation["updated_at"], _json(reservation)))
        return reservation

    def _capacity(self, account, now, ceilings=None):
        balance = self._balance(account, now)
        if ceilings and account["account_id"] in ceilings:
            cap = _units(ceilings[account["account_id"]])
            if set(cap) != set(account["limits"]):
                raise ValueError("capacity override must cover every dimension")
            cap = {k: min(cap[k], account["limits"][k]) for k in cap}
            balance["available"] = {k: cap[k] - balance["held"][k] - balance["spent"][k] for k in cap}
        return balance

    def reserve(self, account_id, idempotency_key, amounts, *, ttl_seconds, actor, reason,
                additional_accounts=(), ceilings=None, known_free=False, context=None):
        account_id, key = _text(account_id, "account_id"), _text(idempotency_key, "idempotency_key")
        amounts, ttl = _units(amounts), _duration(ttl_seconds)
        actor, reason = _text(actor, "actor"), _text(reason, "reason")
        if not any(amounts.values()):
            raise ValueError("reservation must hold at least one unit")
        if type(known_free) is not bool or (known_free and amounts.get("cost_micros", 0)):
            raise ValueError("known-free declarations require a boolean and zero held money")
        if not known_free and "cost_micros" in amounts and not amounts["cost_micros"] and any(
                value for key, value in amounts.items() if key != "cost_micros"):
            raise ValueError("a metered action requires a positive conservative cost hold")
        roots = sorted({_text(value, "additional_account") for value in additional_accounts})
        ceilings = {} if ceilings is None else {str(k): _units(v) for k, v in ceilings.items()}
        context = {} if context is None else dict(context)
        if set(context) - {"task_id", "run_id", "kind", "component", "phase", "effect_id"}:
            raise ValueError("unknown execution reference")
        context = {k: _text(v, k) for k, v in context.items()}
        request = [account_id, key, amounts, ttl]
        if roots or ceilings or known_free or context:
            request += [roots, ceilings, known_free, context]
        fingerprint = hashlib.sha256(_json(request).encode()).hexdigest()
        with self._write():
            old = self.db._conn.execute("SELECT reservation_id,fingerprint FROM budget_reservations "
                "WHERE account_id=? AND idem_key=?", (account_id, key)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise ReservationConflict("idempotency key reused with a different request")
                return self._get(old["reservation_id"])
            accounts = self._lineage(account_id)
            seen = {a["account_id"] for a in accounts}
            for root in roots:
                for account in self._lineage(root):
                    if account["account_id"] not in seen:
                        accounts.append(account)
                        seen.add(account["account_id"])
            if set(ceilings) - seen:
                raise ValueError("override references an unallocated account")
            if set(amounts) != set(accounts[0]["limits"]):
                raise ValueError("reservation must explicitly specify every account dimension")
            now = self._now()
            if not math.isfinite(now + ttl):
                raise ValueError("lease deadline is not finite")
            for account in accounts:
                if set(amounts) != set(account["limits"]) or account["currency"] != accounts[0]["currency"]:
                    raise ValueError("all allocated accounts must use identical dimensions and currency")
                balance = self._capacity(account, now, ceilings)
                if balance["reconciliation_required"]:
                    raise BudgetExceeded("reconciliation_required", "unresolved charge blocks new spending",
                                         scope=account["account_id"])
                for dimension, value in amounts.items():
                    if value > balance["available"][dimension]:
                        raise BudgetExceeded(dimension, "insufficient unreserved capacity",
                                             scope=account["account_id"])
            reservation = dict(schema_version=1, reservation_id="res_" + uuid.uuid4().hex,
                account_id=account_id, idempotency_key=key, amounts=amounts,
                currency=accounts[0]["currency"], created_at=now, expires_at=now + ttl,
                state="reserved", dispatched=False, actual=None, evidence_ref=None,
                resolution=None, revision=0, ceilings=ceilings, context=context, known_free=known_free,
                account_refs=[{k: a[k] for k in ("account_id", "kind", "reference")} for a in accounts])
            self.db._conn.execute("INSERT INTO budget_reservations VALUES (?,?,?,?,?,?,?,?)",
                (reservation["reservation_id"], account_id, key, fingerprint, "reserved", 0,
                 now + ttl, _json(reservation)))
            self.db._conn.executemany("INSERT INTO budget_reservation_accounts VALUES (?,?)",
                [(reservation["reservation_id"], a["account_id"]) for a in accounts])
            return self._save(reservation, "reserved", actor, reason)

    def mark_dispatched(self, reservation_id, *, actor, reason, ceilings=None, authority_check=None):
        actor, reason = _text(actor, "actor"), _text(reason, "reason")
        with self._write():
            if authority_check:
                authority_check()
            reservation = self._get(reservation_id)
            if (reservation["state"] != "reserved" or reservation["dispatched"]
                    or reservation["expires_at"] <= self._now()):
                raise ReservationConflict("dispatch already claimed, expired, or no longer reserved; reconcile")
            # A sibling may have overrun or become unknown AFTER this hold was
            # created. Recheck all ancestors at the last boundary before issuing.
            caps = dict(reservation.get("ceilings") or {})
            for key, values in (ceilings or {}).items():
                checked = _units(values)
                old = caps.get(key, checked)
                if set(old) != set(checked):
                    raise ValueError("changed capacity dimensions")
                caps[key] = {k: min(old[k], checked[k]) for k in old}
            for ref in reservation["account_refs"]:
                account = self._account(ref["account_id"])
                balance = self._capacity(account, self._now(), caps)
                if balance["reconciliation_required"] or any(v < 0 for v in balance["available"].values()):
                    raise BudgetExceeded("reconciliation_required", "account cannot dispatch new work",
                                         scope=account["account_id"])
            reservation["dispatched"] = True
            return self._save(reservation, "dispatched", actor, reason)

    def mark_unknown(self, reservation_id, *, evidence_ref, actor, reason):
        evidence_ref = _text(evidence_ref, "evidence_ref")
        actor, reason = _text(actor, "actor"), _text(reason, "reason")
        with self._write():
            reservation = self._get(reservation_id)
            if reservation["state"] == "unknown" and reservation["evidence_ref"] == evidence_ref:
                return reservation
            if reservation["state"] != "reserved" or not reservation["dispatched"]:
                raise ReservationConflict("only a dispatched pending operation can become unknown")
            reservation.update(state="unknown", evidence_ref=evidence_ref)
            return self._save(reservation, "unknown", actor, reason)

    def refund(self, reservation_id, *, actor, reason):
        actor, reason = _text(actor, "actor"), _text(reason, "reason")
        with self._write():
            reservation = self._get(reservation_id)
            if reservation["state"] == "refunded":
                return reservation
            if reservation["state"] != "reserved" or reservation["dispatched"]:
                raise ReservationConflict("dispatched or unknown charge requires evidenced reconciliation")
            reservation.update(state="refunded", resolution="refund")
            return self._save(reservation, "refunded", actor, reason)

    def _complete(self, reservation_id, actual, evidence_ref, actor, reason, operation):
        actual = _units(actual)
        evidence_ref = _text(evidence_ref, "evidence_ref")
        actor, reason = _text(actor, "actor"), _text(reason, "reason")
        with self._write():
            reservation = self._get(reservation_id)
            if set(actual) != set(reservation["amounts"]):
                raise ValueError("actual usage must cover every dimension; unknown is not zero")
            if reservation["state"] in {"settled", "refunded"}:
                if (reservation["actual"] == actual and reservation["evidence_ref"] == evidence_ref
                        and reservation["resolution"] == operation):
                    return reservation
                raise ReservationConflict("terminal settlement cannot be changed or billed again")
            expected = "unknown" if operation == "reconcile" else "reserved"
            if reservation["state"] != expected or not reservation["dispatched"]:
                raise ReservationConflict("settle dispatched pending work; reconcile unknown work explicitly")
            reservation.update(state="settled" if any(actual.values()) else "refunded",
                actual=actual, evidence_ref=evidence_ref, resolution=operation,
                overrun={k: max(0, actual[k] - reservation["amounts"][k]) for k in actual})
            # Record real overruns even beyond capacity; never hide a provider charge.
            # Mirror named accounts only, not legacy runtime/provider pools twice.
            for ref in reservation["account_refs"]:
                scope = f"reservation:{reservation['currency']}:{ref['account_id']}"
                for key in self.ledger._keys(scope, reservation["created_at"]):
                    self.db._conn.execute("INSERT INTO budget_ledger VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(ledger_key) DO UPDATE SET "
                        "model_calls=model_calls+excluded.model_calls, "
                        "tool_calls=tool_calls+excluded.tool_calls,cost=cost+excluded.cost, "
                        "updated_at=excluded.updated_at",
                        (key, actual.get("model_calls", 0), actual.get("tool_calls", 0),
                         actual.get("cost_micros", 0) / 1_000_000, 0, self._now()))
            return self._save(reservation, operation, actor, reason)

    def settle(self, reservation_id, actual, *, evidence_ref, actor, reason):
        return self._complete(reservation_id, actual, evidence_ref, actor, reason, "settle")

    def reconcile(self, reservation_id, actual, *, evidence_ref, actor, reason):
        return self._complete(reservation_id, actual, evidence_ref, actor, reason, "reconcile")

    def recover_expired(self, *, actor, reason):
        actor, reason = _text(actor, "actor"), _text(reason, "reason")
        with self._write():
            rows = self.db._conn.execute("SELECT data FROM budget_reservations "
                "WHERE state='reserved' AND expires_at<=? ORDER BY reservation_id", (self._now(),)).fetchall()
            results = []
            for row in rows:
                reservation = json.loads(row[0])
                if reservation["dispatched"]:
                    reservation.update(state="unknown", evidence_ref="recovery:dispatch-expired")
                else:
                    reservation.update(state="refunded", resolution="expired_unused")
                results.append(self._save(reservation, reservation["state"], actor, reason))
            return results

    def history(self, reservation_id):
        with self.db._lock:
            self._get(reservation_id)
            rows = self.db._conn.execute("SELECT * FROM budget_reservation_events "
                "WHERE reservation_id=? ORDER BY revision", (reservation_id,)).fetchall()
            return [{**dict(row), "data": json.loads(row["data"])} for row in rows]
