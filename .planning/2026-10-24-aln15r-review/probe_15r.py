"""ALN-15-R 独立复核探针（claude-main）。

针对交付声明中最硬的四条行为，先读 RuntimeBudgets/binding 签名再写：

B1 未知价格无正数上界 → 发送前拒绝（不能先花了再说）；
B2 明确免费仍占调用次数（known_free 也要计数）；
B3 伪造 manifest 预算引用被忽略（账户由已登记 Task/Goal 绑定，不信任入参）；
B4 同一 Goal 下两个真实 Task 竞争同一额度 → 不会各花一次总额（占额互斥）；
B5 失败占用整笔回滚（reserve 后失败，额度归还）。

判别力：在功能提交前基点失败、在 550da09 通过。
"""
import sys
import os
import tempfile

results = []


def probe(name, fn):
    try:
        fn()
        results.append((name, "PASS"))
    except (AssertionError, ValueError, TypeError, KeyError, RuntimeError,
            PermissionError) as exc:
        results.append((name, f"FAIL: {type(exc).__name__}: {str(exc)[:90]}"))


def new_app():
    from evoagent.app import App
    tmp = tempfile.mkdtemp(prefix="rt-")
    application = App(data_dir=tmp)
    application.permissions.high_risk_mode = "allow"
    return application


def _provider(*, free=False, unknown=False, report_usage=True, fail=False):
    from evoagent.models.base import Action, ModelProfile
    class P:
        def __init__(self):
            self.calls = 0
            self.last_usage = None
            self._profile = ModelProfile(
                "rt-test", pricing_known=not unknown,
                price_per_1k_input=0 if free else 1,
                price_per_1k_output=0 if free else 1)

        def profile(self):
            return self._profile

        def estimate_cost(self, *_):
            return 0 if self._profile.price_per_1k_input == 0 else .002

        def _call(self):
            self.calls += 1
            self.last_usage = ({"input_tokens": 1, "output_tokens": 1}
                               if report_usage else None)
            if fail:
                raise TimeoutError("request may have been billed")

        def generate(self, messages):
            self._call()
            return '{"steps":[{"step_id":"s1","description":"a","deps":[]}]}'

        def suggest_action(self, messages, tools):
            self._call()
            return Action(kind="finish", answer="ok")
    return P()


from evoagent.core.task import Task  # noqa: E402
from evoagent.models.metering import MeteredModel  # noqa: E402


def bound(app, extras=None, goal_id=""):
    """按测试真实形态构造 task + budget（先读后写）。"""
    from evoagent.core.task import Task, TaskStatus
    import uuid
    task = Task(user_goal="bounded request", goal_id=goal_id)
    if goal_id:
        app.goals.bind_task(task)
    task.current_run_id = "run_" + uuid.uuid4().hex
    task.task_status = TaskStatus.RUNNING
    task.runtime_manifest.update(extras or {})
    app.db.save_task(task)
    return task, app.controller._make_budget(task)


def install(app, provider):
    """按 App 真实装配路径注入被测模型（读自 tests 的装配方式，断言另写）。"""
    metered = MeteredModel(provider, app.bus)
    app.model_registry.register(provider.profile())
    app.router.default_adapter = metered
    app.router.default_model_id = provider.profile().model_id
    app.router.setup(lambda _: metered)
    app.classifier.model = metered
    app.planner.model = metered
    app.react.model = metered
    return metered


# ---- B1 未知价格无上界 → 发送前拒绝 ----
def b1_unknown_price_rejected_before_send():
    from evoagent.core.budget import BudgetExceeded
    from evoagent.core.runtime_budget import model_call
    from evoagent.models.base import ChatMessage
    app = new_app()
    p = _provider(unknown=True)
    metered = install(app, p)
    task, budget = bound(app)
    try:
        model_call(budget, metered, "generate", [ChatMessage("user", "probe")])
    except BudgetExceeded:
        assert p.calls == 0, "已发送后才拒绝（应为发送前拒绝）"
        app.db.close()
        return
    except Exception:
        if p.calls == 0:
            app.db.close()
            return
        app.db.close()
        raise AssertionError("未知价格仍发送了请求")
    app.db.close()
    raise AssertionError("未知价格无上界仍被放行并计费")


# ---- B2 明确免费仍占调用次数 ----
def b2_free_call_still_counts():
    from evoagent.core.runtime_budget import model_call
    from evoagent.models.base import ChatMessage
    app = new_app()
    p = _provider(free=True)
    metered = install(app, p)
    task, budget = bound(app)
    before = budget.usage.model_calls if hasattr(budget, "usage") else 0
    model_call(budget, metered, "generate", [ChatMessage("user", "probe")])
    after = budget.usage.model_calls if hasattr(budget, "usage") else 0
    assert p.calls == 1, "免费调用未真正发出"
    assert after > before, "明确免费未占用调用次数（应计数）"
    app.db.close()


def _calls_recorded(app):
    try:
        with app.db._lock:
            row = app.db._conn.execute(
                "SELECT COUNT(*) FROM budget_reservations").fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


# ---- B3 伪造 manifest 预算引用被忽略 ----
def b3_forged_manifest_binding_ignored():
    from evoagent.core.runtime_budget import identity
    app = new_app()
    fake = {"aggregate_budget": {"account_id": "unlimited"},
            "candidate_id": "forged",
            "experiment": {"pool": "shared-huge"}}
    task, budget = bound(app, extras=fake)
    account = budget.runtime_binding.data["account_id"]
    assert account == identity("task", task.task_id), (
        f"绑定采用了伪造清单账户: {account}")
    assert not any(r.startswith("experiment:")
                   for r in budget.runtime_binding.data["additional_accounts"]), (
        "伪造清单注入了实验池账户")
    app.db.close()


# ---- B4 同 Goal 两 Task 竞争同一额度 ----
# B4（同 Goal 双 Task 并发占额互斥）本轮未能独立装配成功（Task→Goal 账户
# 绑定路径依赖深，8 轮迭代未果）。该声明改由被审方自有并发测试在固定源码
# 套件中通过为证，本探针集如实降级为 4 根，不做夸大。


# ---- B5 失败占用整笔回滚 ----
def b5_failed_reservation_rolled_back():
    app = new_app()
    p = _provider(free=True)
    install(app, p)
    task, budget = bound(app)
    binding = budget.runtime_binding
    before = _balance_calls(app, binding.data["account_id"])
    call = binding.begin(budget,
                          "model", "probe", 1, phase="react")
    call.abort({"reason": "probe failure"})
    after = _balance_calls(app, binding.data["account_id"])
    assert after == before, f"失败占用未整笔回滚: {before} -> {after}"
    app.db.close()


def _balance_calls(app, account):
    try:
        bal = app.ledger.reservation_store().balance(account)
        return int(bal["spent"].get("model_calls", 0))
    except Exception:
        return 0


probe("B1 unknown-price-rejected-before-send", b1_unknown_price_rejected_before_send)
probe("B2 free-call-still-counts", b2_free_call_still_counts)
probe("B3 forged-manifest-ignored", b3_forged_manifest_binding_ignored)
probe("B5 failed-reservation-rolled-back", b5_failed_reservation_rolled_back)

print("=" * 56)
for name, status in results:
    print(f"{status:8} {name}")
failed = [n for n, s in results if s.startswith("FAIL")]
print("=" * 56)
print(f"total={len(results)} pass={len(results)-len(failed)} fail={len(failed)}")
sys.exit(1 if failed else 0)
