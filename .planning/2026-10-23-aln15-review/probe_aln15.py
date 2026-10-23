"""ALN-15-A 独立验收探针（reviewer: claude-main，2026-10-23）。

按 cwd 加载被审源码：在干净 worktree（6976662）根目录执行本脚本。
不复用被审方的 tests/，全部行为独立复现；覆盖任务卡五条验收标准：
  1. 同一额度并发预留不双重占用；超限拒绝且原因可查；
  2. 调用前持久化；成功按实测结算、失败退款、unknown 对账不清零；
  3. 崩溃后遗留预留可识别、可回收/对账、不重复计费；
  4. Goal/Task/实验引用同一账本；子目标不扩大父额度；
  5. 边界：终态不重放、dispatch 只领一次、unknown 不按零、超支如实记账。
"""
import multiprocessing
import os
import sys
import threading
import time

sys.path.insert(0, os.getcwd())  # 审计语义：导入 cwd（被审 checkout）的源码

from evoagent.core.budget import BudgetExceeded, BudgetLedger  # noqa: E402
from evoagent.core.persistence import Database  # noqa: E402
from evoagent.core.reservations import ReservationConflict  # noqa: E402

AUDIT = {"actor": "review:probe", "reason": "independent acceptance"}
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + ((" | " + detail) if detail else ""))


def new_store(path, clock=None):
    db = Database(str(path))
    service = BudgetLedger(db).reservation_store(clock=clock)
    return db, service


def make_account(store, name, limits, parent=None, kind="goal", currency="USD"):
    return store.create_account(name, limits, kind=kind, reference=name,
                                parent_id=parent, currency=currency, **AUDIT)


LIMITS = {"cost_micros": 100, "model_calls": 10}


# ---------- P1/P2/P3：并发预留不双重占用 ----------

def _thread_race_two_connections(path):
    """两个独立连接（同文件）多线程争抢同一额度。库与账户须预先建好。"""
    ok = []
    barrier = threading.Barrier(6)

    def attempt(i):
        try:
            db, store = new_store(path)
        except Exception as exc:  # 开库失败不得拖死 barrier
            ok.append(("open-failed", repr(exc)))
            try:
                barrier.wait(timeout=30)
            except threading.BrokenBarrierError:
                pass
            return
        try:
            barrier.wait(timeout=30)
            try:
                store.reserve("goal:root", f"call:{i}", {"cost_micros": 60, "model_calls": 1},
                              ttl_seconds=60, **AUDIT)
                ok.append(i)
            except BudgetExceeded:
                pass
        finally:
            db.close()

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return ok


def _process_worker(path, key):
    db, store = new_store(path)
    try:
        store.reserve("goal:root", key, {"cost_micros": 60, "model_calls": 1},
                      ttl_seconds=60, **AUDIT)
        print("worker-win", key)
    except BudgetExceeded:
        pass
    finally:
        db.close()


def _process_race(path):
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_process_worker, args=(path, f"call:p{i}")) for i in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    return [p.exitcode for p in procs]


# ---------- P7：崩溃后遗留预留 ----------

def _crash_after_dispatch(path):
    db, store = new_store(path)
    res = store.reserve("goal:root", "call:crash", {"cost_micros": 60, "model_calls": 1},
                        ttl_seconds=2, **AUDIT)
    store.mark_dispatched(res["reservation_id"], **AUDIT)
    os._exit(23)  # 提交后直接死，不关库


def probe_concurrency(tmp):
    db, store = new_store(tmp / "race.db")
    make_account(store, "goal:root", LIMITS)

    barrier = threading.Barrier(8)
    winners = []

    def attempt(i):
        barrier.wait(timeout=30)
        try:
            store.reserve("goal:root", f"call:{i}", {"cost_micros": 60, "model_calls": 1},
                          ttl_seconds=60, **AUDIT)
            winners.append(i)
        except BudgetExceeded:
            pass

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    bal = store.balance("goal:root")
    check("P1 单连接8线程只允许1个预留", len(winners) == 1 and bal["held"]["cost_micros"] == 60,
          f"winners={winners} held={bal['held']}")
    db.close()

    db2, store2 = new_store(tmp / "race.db")  # 重开验证持久化只占一份
    check("P1b 重开后占用仍为60不翻倍", store2.balance("goal:root")["held"]["cost_micros"] == 60)
    db2.close()

    # 双连接赛跑：先建库和账户，再开两个独立连接争抢（支持 pattern：建一次后竞争）
    dbA, storeA = new_store(tmp / "race3.db")
    make_account(storeA, "goal:root", LIMITS)
    dbA.close()
    winners2 = _thread_race_two_connections(tmp / "race3.db")
    dbB, storeB = new_store(tmp / "race3.db")
    bal = storeB.balance("goal:root")
    check("P2 双连接6线程只允许1个预留",
          len(winners2) == 1 and bal["held"]["cost_micros"] == 60
          and not any(isinstance(w, tuple) for w in winners2), f"winners={winners2}")
    dbB.close()

    codes = _process_race(tmp / "race3.db")
    dbC, storeC = new_store(tmp / "race3.db")
    bal = storeC.balance("goal:root")
    reserved = storeC.db._conn.execute(
        "SELECT COUNT(*) c FROM budget_reservations WHERE state='reserved'").fetchone()["c"]
    check("P3 4进程争抢后库内恰好1笔预留", reserved == 1 and bal["held"]["cost_micros"] == 60,
          f"reserved={reserved} exitcodes={codes}")
    dbC.close()


def probe_rejection_and_durability(tmp):
    db, store = new_store(tmp / "reject.db")
    make_account(store, "goal:root", LIMITS)
    try:
        store.reserve("goal:root", "call:big", {"cost_micros": 101, "model_calls": 1},
                      ttl_seconds=60, **AUDIT)
        check("P4 超限预留被拒绝", False)
    except BudgetExceeded as exc:
        check("P4 超限预留被拒绝且原因可查",
              exc.dimension == "cost_micros" and exc.scope == "goal:root", str(exc))
    # 充值维度超限
    try:
        store.reserve("goal:root", "call:calls", {"cost_micros": 1, "model_calls": 11},
                      ttl_seconds=60, **AUDIT)
        check("P4b 调用量超限被拒绝", False)
    except BudgetExceeded as exc:
        check("P4b 调用量超限被拒绝", exc.dimension == "model_calls", str(exc))

    res = store.reserve("goal:root", "call:dur", {"cost_micros": 60, "model_calls": 1},
                        ttl_seconds=60, **AUDIT)
    db.close()  # 不结算直接关库 = 进程消失
    db2, store2 = new_store(tmp / "reject.db")
    bal = store2.balance("goal:root")
    check("P5 调用前预留已持久化（重启后仍占用）",
          bal["held"]["cost_micros"] == 60 and bal["spent"]["cost_micros"] == 0,
          f"held={bal['held']}")
    try:
        store2.reserve("goal:root", "call:after", {"cost_micros": 60, "model_calls": 1},
                       ttl_seconds=60, **AUDIT)
        check("P5b 重启后预留仍挡住同额新预留", False)
    except BudgetExceeded:
        check("P5b 重启后预留仍挡住同额新预留", True)
    db2.close()


def probe_lifecycle(tmp):
    db, store = new_store(tmp / "life.db", clock=lambda: 1700000000.0)
    make_account(store, "goal:root", LIMITS)

    # 未领取 → 退款释放
    r1 = store.reserve("goal:root", "call:refund", {"cost_micros": 60, "model_calls": 1},
                       ttl_seconds=60, **AUDIT)
    store.refund(r1["reservation_id"], **AUDIT)
    bal = store.balance("goal:root")
    check("P6a 未领取退款释放占用", bal["held"]["cost_micros"] == 0
          and bal["available"]["cost_micros"] == 100)

    # 已领取 → 拒绝退款，只能 unknown 对账
    r2 = store.reserve("goal:root", "call:unknown", {"cost_micros": 60, "model_calls": 1},
                       ttl_seconds=60, **AUDIT)
    store.mark_dispatched(r2["reservation_id"], **AUDIT)
    try:
        store.refund(r2["reservation_id"], **AUDIT)
        check("P6b 已领取拒绝退款", False)
    except ReservationConflict:
        check("P6b 已领取拒绝退款", True)
    store.mark_unknown(r2["reservation_id"], evidence_ref="provider:timeout", **AUDIT)
    bal = store.balance("goal:root")
    check("P6c unknown 保持占用且要求对账", bal["held"]["cost_micros"] == 60
          and bal["unknown_count"] == 1 and bal["reconciliation_required"])
    try:
        store.reserve("goal:root", "call:blocked", {"cost_micros": 1, "model_calls": 1},
                      ttl_seconds=60, **AUDIT)
        check("P6d unknown 未对账前阻断新动作", False)
    except BudgetExceeded:
        check("P6d unknown 未对账前阻断新动作", True)
    store.reconcile(r2["reservation_id"], {"cost_micros": 25, "model_calls": 1},
                    evidence_ref="invoice:final", **AUDIT)
    bal = store.balance("goal:root")
    check("P6e 对账后按实测落地、占用释放",
          bal["held"]["cost_micros"] == 0 and bal["spent"]["cost_micros"] == 25
          and not bal["reconciliation_required"], f"spent={bal['spent']}")
    db.close()

    # 旧账本镜像：对账后恰好入账一次（日+月两键，金额各一次）
    db3, store3 = new_store(tmp / "life.db")
    rows = db3._conn.execute(
        "SELECT ledger_key, model_calls, cost FROM budget_ledger "
        "WHERE ledger_key LIKE 'reservation:USD:%' ORDER BY ledger_key").fetchall()
    # 账本 cost 为币种单位：cost_micros=25 → 25/1e6
    check("P6f 对账镜像入账一次（日/月各一笔、金额一致）",
          len(rows) == 2 and all(r["cost"] == 25 / 1_000_000 for r in rows)
          and all(r["model_calls"] == 1 for r in rows), str([dict(r) for r in rows]))
    db3.close()


def probe_crash(tmp):
    path = tmp / "crash.db"
    db, store = new_store(path)
    make_account(store, "goal:root", LIMITS)
    db.close()

    ctx = multiprocessing.get_context("spawn")
    worker = ctx.Process(target=_crash_after_dispatch, args=(str(path),))
    worker.start()
    worker.join(30)
    check("P7a 子进程以非零码崩溃", worker.exitcode == 23, f"exitcode={worker.exitcode}")

    db2, store2 = new_store(path)
    bal = store2.balance("goal:root")
    check("P7b 崩溃后预留仍持久占用", bal["held"]["cost_micros"] == 60, f"held={bal['held']}")
    # 未过期的在途占用是正常 pending：不阻断额度内的新动作（冻结整个账户才是缺陷）
    try:
        r = store2.reserve("goal:root", "call:new", {"cost_micros": 1, "model_calls": 1},
                           ttl_seconds=60, **AUDIT)
        store2.mark_dispatched(r["reservation_id"], **AUDIT)
        check("P7c 在途（未过期）占用不冻结账户，额度内新动作放行", True)
    except (BudgetExceeded, ReservationConflict):
        check("P7c 在途（未过期）占用不冻结账户，额度内新动作放行", False)

    time.sleep(2.2)  # ttl=2 真实时钟过期
    # 过期后、恢复扫描执行前：已领取过期记录必须阻断新 dispatch
    try:
        r = store2.reserve("goal:root", "call:blocked", {"cost_micros": 1, "model_calls": 1},
                           ttl_seconds=60, **AUDIT)
        store2.mark_dispatched(r["reservation_id"], **AUDIT)
        check("P7c2 恢复扫描前已领取过期记录阻断新 dispatch", False)
    except (BudgetExceeded, ReservationConflict):
        check("P7c2 恢复扫描前已领取过期记录阻断新 dispatch", True)
    recovered = store2.recover_expired(**AUDIT)
    states = [(r["reservation_id"][:12], r["state"], r.get("evidence_ref")) for r in recovered]
    bal = store2.balance("goal:root")
    check("P7d 过期已领取转 unknown 而非退款",
          len(recovered) == 1 and recovered[0]["state"] == "unknown"
          and recovered[0]["evidence_ref"] == "recovery:dispatch-expired"
          and bal["held"]["cost_micros"] == 61 and bal["reconciliation_required"], str(states))
    store2.reconcile(recovered[0]["reservation_id"], {"cost_micros": 60, "model_calls": 1},
                     evidence_ref="invoice:crash", **AUDIT)
    rows = db2._conn.execute(
        "SELECT ledger_key, cost FROM budget_ledger WHERE ledger_key LIKE 'reservation:USD:%'"
    ).fetchall()
    total = sum(r["cost"] for r in rows)
    # 日+月两键各入账一次：total = 2 × (60 micros / 1e6)
    check("P7e 崩溃预留对账后入账一次不重复计费",
          len(rows) == 2 and abs(total - 2 * 60 / 1_000_000) < 1e-12,
          f"rows={len(rows)} total={total}")
    # 终态重放不得二次入账
    store2.reconcile(recovered[0]["reservation_id"], {"cost_micros": 60, "model_calls": 1},
                     evidence_ref="invoice:crash", **AUDIT)
    rows2 = db2._conn.execute(
        "SELECT cost FROM budget_ledger WHERE ledger_key LIKE 'reservation:USD:%'").fetchall()
    check("P7f 对账重放不二次入账", len(rows2) == len(rows)
          and abs(sum(r["cost"] for r in rows2) - total) < 1e-12)
    db2.close()


def probe_hierarchy(tmp):
    db, store = new_store(tmp / "hier.db")
    make_account(store, "goal:root", {"cost_micros": 100, "model_calls": 10})
    make_account(store, "task:a", {"cost_micros": 60, "model_calls": 10}, parent="goal:root",
                 kind="task")
    make_account(store, "task:b", {"cost_micros": 60, "model_calls": 10}, parent="goal:root",
                 kind="task")
    make_account(store, "exp:1", {"cost_micros": 60, "model_calls": 5}, parent="task:a",
                 kind="experiment")
    store.reserve("task:a", "call:a1", {"cost_micros": 60, "model_calls": 1},
                  ttl_seconds=60, **AUDIT)
    try:
        store.reserve("task:b", "call:b1", {"cost_micros": 60, "model_calls": 1},
                      ttl_seconds=60, **AUDIT)
        check("P8a 兄弟任务不能双花父额度", False)
    except BudgetExceeded:
        check("P8a 兄弟任务不能双花父额度", True)
    bal_root = store.balance("goal:root")
    check("P8b 父账户占用同步可见", bal_root["held"]["cost_micros"] == 60,
          f"root held={bal_root['held']}")
    # 子账户不得扩大父额度
    for bad, label in (
        ({"cost_micros": 101, "model_calls": 10}, "超父额度"),
        ({"cost_micros": 60}, "缺维度"),
        ({"cost_micros": 60, "model_calls": 10, "tokens": 1}, "加维度"),
    ):
        try:
            make_account(store, "task:bad", bad, parent="goal:root", kind="task")
            check(f"P8c 子账户拒绝{label}", False)
        except ValueError:
            check(f"P8c 子账户拒绝{label}", True)
    try:
        make_account(store, "task:cur", {"cost_micros": 60, "model_calls": 10},
                     parent="goal:root", kind="task", currency="EUR")
        check("P8d 子账户币种须与父一致", False)
    except ValueError:
        check("P8d 子账户币种须与父一致", True)
    # 三级引用可追溯到同一账本（用独立层级，避免与上面的占用互相污染）
    make_account(store, "root:h2", {"cost_micros": 100, "model_calls": 10})
    make_account(store, "task:h2", {"cost_micros": 60, "model_calls": 10}, parent="root:h2",
                 kind="task")
    make_account(store, "exp:h2", {"cost_micros": 60, "model_calls": 5}, parent="task:h2",
                 kind="experiment")
    r = store.reserve("exp:h2", "call:e1", {"cost_micros": 30, "model_calls": 1},
                      ttl_seconds=60, **AUDIT)
    refs = {a["account_id"] for a in r["account_refs"]}
    check("P8e 三级引用绑定同一预留", refs == {"exp:h2", "task:h2", "root:h2"}, str(refs))
    db.close()


def probe_terminal_and_dispatch(tmp):
    db, store = new_store(tmp / "term.db")
    make_account(store, "goal:root", {"cost_micros": 1000, "model_calls": 100})
    r = store.reserve("goal:root", "call:t", {"cost_micros": 100, "model_calls": 2},
                      ttl_seconds=60, **AUDIT)
    store.mark_dispatched(r["reservation_id"], **AUDIT)
    first = store.settle(r["reservation_id"], {"cost_micros": 40, "model_calls": 1},
                         evidence_ref="invoice:1", **AUDIT)
    again = store.settle(r["reservation_id"], {"cost_micros": 40, "model_calls": 1},
                         evidence_ref="invoice:1", **AUDIT)
    check("P9a 终态相同重放返回同一记录", again["revision"] == first["revision"]
          and again["state"] == "settled", f"rev={again['revision']}")
    try:
        store.settle(r["reservation_id"], {"cost_micros": 41, "model_calls": 1},
                     evidence_ref="invoice:1", **AUDIT)
        check("P9b 终态不同重放被拒", False)
    except ReservationConflict:
        check("P9b 终态不同重放被拒", True)
    rows = db._conn.execute(
        "SELECT cost FROM budget_ledger WHERE ledger_key LIKE 'reservation:USD:%'").fetchall()
    check("P9c 重复结算不重复入账", abs(sum(r_["cost"] for r_ in rows) - 2 * 40 / 1_000_000) < 1e-12,
          f"total={sum(r_['cost'] for r_ in rows)}")

    r2 = store.reserve("goal:root", "call:d", {"cost_micros": 100, "model_calls": 2},
                       ttl_seconds=60, **AUDIT)
    store.mark_dispatched(r2["reservation_id"], **AUDIT)
    try:
        store.mark_dispatched(r2["reservation_id"], **AUDIT)
        check("P10a dispatch 只领一次", False)
    except ReservationConflict:
        check("P10a dispatch 只领一次", True)
    r3 = store.reserve("goal:root", "call:s", {"cost_micros": 100, "model_calls": 2},
                       ttl_seconds=60, **AUDIT)
    try:
        store.settle(r3["reservation_id"], {"cost_micros": 10, "model_calls": 1},
                     evidence_ref="invoice:x", **AUDIT)
        check("P10b 未 dispatch 不得结算", False)
    except ReservationConflict:
        check("P10b 未 dispatch 不得结算", True)
    r4 = store.reserve("goal:root", "call:u", {"cost_micros": 100, "model_calls": 2},
                       ttl_seconds=60, **AUDIT)
    try:
        store.mark_unknown(r4["reservation_id"], evidence_ref="e", **AUDIT)
        check("P10c 未 dispatch 不得标记 unknown", False)
    except ReservationConflict:
        check("P10c 未 dispatch 不得标记 unknown", True)

    # unknown 不是零：缺维度实测被拒
    try:
        store.settle(r2["reservation_id"], {"cost_micros": 10},
                     evidence_ref="invoice:y", **AUDIT)
        check("P11a 缺维度实测被拒（unknown 不按零）", False)
    except ValueError:
        check("P11a 缺维度实测被拒（unknown 不按零）", True)
    # 计量动作必须给正数保守费用
    try:
        store.reserve("goal:root", "call:zero", {"cost_micros": 0, "model_calls": 1},
                      ttl_seconds=60, **AUDIT)
        check("P11b 零费用计量预留被拒", False)
    except ValueError:
        check("P11b 零费用计量预留被拒", True)
    # 布尔值不当整数
    try:
        store.reserve("goal:root", "call:bool", {"cost_micros": True, "model_calls": 1},
                      ttl_seconds=60, **AUDIT)
        check("P11c 布尔金额被拒", False)
    except ValueError:
        check("P11c 布尔金额被拒", True)

    # 超支如实记账并停止后续
    over = store.settle(r2["reservation_id"], {"cost_micros": 1500, "model_calls": 3},
                        evidence_ref="invoice:over", **AUDIT)
    bal = store.balance("goal:root")
    check("P12a 超支如实结算且记录 overrun",
          over["overrun"]["cost_micros"] == 1400 and bal["spent"]["cost_micros"] == 1540
          and bal["available"]["cost_micros"] < 0, f"available={bal['available']}")
    try:
        store.reserve("goal:root", "call:after-over", {"cost_micros": 1, "model_calls": 1},
                      ttl_seconds=60, **AUDIT)
        check("P12b 负余额后停止新预留", False)
    except BudgetExceeded:
        check("P12b 负余额后停止新预留", True)
    db.close()


def probe_expiry_semantics(tmp):
    clock = [1700000000.0]
    db, store = new_store(tmp / "expiry.db", clock=lambda: clock[0])
    make_account(store, "goal:root", LIMITS)
    r = store.reserve("goal:root", "call:exp", {"cost_micros": 60, "model_calls": 1},
                      ttl_seconds=10, **AUDIT)
    clock[0] += 11  # 过期但未领取
    bal = store.balance("goal:root")
    check("P13a 未领取过期不要求对账（只占额度）",
          not bal["reconciliation_required"] and bal["held"]["cost_micros"] == 60)
    try:
        store.mark_dispatched(r["reservation_id"], **AUDIT)
        check("P13b 过期后不得 dispatch", False)
    except ReservationConflict:
        check("P13b 过期后不得 dispatch", True)
    store.reserve("goal:root", "call:small", {"cost_micros": 40, "model_calls": 1},
                  ttl_seconds=10, **AUDIT)
    check("P13c 未领取过期不阻断新预留（保守占用但可放行）", True)
    recovered = store.recover_expired(**AUDIT)
    states = sorted({x["state"] for x in recovered})
    bal = store.balance("goal:root")
    # 回收后：过期未领取退款释放 60，仅剩 call:small 的 40 占用 → available=100-40=60
    check("P13d 回收：未领取过期退款、占用释放",
          states == ["refunded"] and bal["held"]["cost_micros"] == 40
          and bal["available"]["cost_micros"] == 60, str(states))
    db.close()


def main():
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp(prefix="aln15-probe-"))
    print("probe dir:", tmp, flush=True)
    for name, fn in (("concurrency", probe_concurrency),
                     ("rejection+durability", probe_rejection_and_durability),
                     ("lifecycle", probe_lifecycle),
                     ("crash", probe_crash),
                     ("hierarchy", probe_hierarchy),
                     ("terminal+dispatch", probe_terminal_and_dispatch),
                     ("expiry", probe_expiry_semantics)):
        print(f"\n--- {name} ---", flush=True)
        fn(tmp)
    failed = [name for name, ok in RESULTS if not ok]
    print(f"\n==== {len(RESULTS) - len(failed)}/{len(RESULTS)} passed ====", flush=True)
    if failed:
        print("FAILED:", failed)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
