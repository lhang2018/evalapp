"""ALN-13-A 独立探针（复核方自写，不复用被审方 tests/）。

断言依据：任务卡 13-Goal与自主授权.md 验收段 + docs/协作开发/设计/ALN-13-Goal与自主授权契约.md。
按 cwd 加载被审源码（sys.path.insert(0, os.getcwd())）。
"""
import inspect
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.getcwd())

from evoagent.core.contracts import GOAL_STATUS_SPEC
from evoagent.core.persistence import Database
from evoagent.core.task import Task
from evoagent.goals import GoalConflict, GoalStore
from evoagent.tools.contract import ToolContract

RESULTS = []


def ok(name, fn):
    try:
        value = fn()
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((False, name, f"意外异常 {type(exc).__name__}: {exc}"))
        return None
    RESULTS.append((True, name, ""))
    return value


def blocked(name, fn, exc_types=(PermissionError,), contains=None):
    try:
        fn()
    except exc_types as exc:
        if contains and contains not in str(exc):
            RESULTS.append((False, name, f"异常类型对但信息不含 {contains!r}: {exc}"))
        else:
            RESULTS.append((True, name, ""))
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((False, name, f"异常类型不符 {type(exc).__name__}: {exc}"))
    else:
        RESULTS.append((False, name, "未拒绝"))


POLICY = {"tools": ["read", "write"], "permissions": [], "max_risk": "critical"}
CONTRACT_NONE = ToolContract("read", side_effect_level="none")
CONTRACT_LOCAL = ToolContract("write", side_effect_level="local")

# 契约「数据与转移」表逐字转录
CONTRACT = {
    "proposed": {"ready", "blocked", "cancelled", "expired"},
    "ready": {"running", "paused", "blocked", "cancelled", "expired"},
    "running": {"waiting", "paused", "blocked", "review", "cancelled", "expired"},
    "waiting": {"ready", "paused", "blocked", "cancelled", "expired"},
    "paused": {"ready", "blocked", "cancelled", "expired"},
    "blocked": {"ready", "paused", "cancelled", "expired"},
    "review": {"ready", "completed", "paused", "blocked", "cancelled", "expired"},
    "completed": set(), "cancelled": set(), "expired": set(),
}


class Env:
    def __init__(self, path):
        self.db = Database(path)
        self.clock = [1_000_000.0]
        self.store = GoalStore(self.db, clock=lambda: self.clock[0])

    def create(self, **kw):
        title = kw.pop("title", "purpose")
        kw.setdefault("deadline", self.clock[0] + 10000)
        return self.store.create(title, actor="user",
                                 success_criteria=["verified"], **kw)

    def authorize(self, goal, level="execution", policy=None, budget=None, expires_at=None):
        return self.store.authorize(
            goal["goal_id"], policy or dict(POLICY), level=level, actor="admin",
            reason="grant", expected_revision=goal["revision"],
            expires_at=expires_at or self.clock[0] + 5000,
            budget_policy=budget)

    def step(self, goal, status, **kw):
        return self.store.transition(
            goal["goal_id"], status, actor="user", reason="step",
            expected_revision=goal["revision"], **kw)


def drive(env, state):
    goal = env.create()
    if state == "proposed":
        # 契约：进入 ready 需有效授权；proposed 本身不变
        return env.authorize(goal)
    goal = env.authorize(goal)
    if state == "expired":
        env.clock[0] += 20000
        return env.step(goal, "expired")
    path = {
        "ready": ["ready"],
        "running": ["ready", "running"],
        "waiting": ["ready", "running", "waiting"],
        "paused": ["ready", "running", "paused"],
        "blocked": ["ready", "blocked"],
        "review": ["ready", "running", "review"],
        "completed": ["ready", "running", "review"],
        "cancelled": ["ready", "cancelled"],
    }[state]
    for status in path:
        goal = env.step(goal, status)
    if state == "completed":
        goal = env.step(goal, "completed", evidence_refs=["evidence:ok"])
    return goal


def main():
    tmp = tempfile.mkdtemp(prefix="aln13probe_")
    env = Env(os.path.join(tmp, "goals.db"))
    store = env.store

    # P01 十态
    RESULTS.append((len(GOAL_STATUS_SPEC) == 10 and set(GOAL_STATUS_SPEC) == set(CONTRACT),
                    "P01a 契约十态齐全", str(GOAL_STATUS_SPEC)))
    blocked("P01b 未知目标状态被拒绝", lambda: env.step(env.create(), "zombie"),
            (ValueError,), "非法Goal状态")

    # P02 全矩阵：契约内成功，契约外 409
    for state in [s for s in GOAL_STATUS_SPEC if s != "expired"]:
        for target in GOAL_STATUS_SPEC:
            legal = target in CONTRACT[state]
            name = f"P02 {state}->{target} {'允许' if legal else '拒绝'}"
            if legal:
                def act(state=state, target=target):
                    goal = drive(env, state)
                    kw = {"evidence_refs": ["evidence:ok"]} if target == "completed" else {}
                    if target == "expired":
                        env.clock[0] += 20000
                    return env.step(goal, target, **kw)
                ok(name, act)
            else:
                def act(state=state, target=target):
                    return env.step(drive(env, state), target)
                blocked(name, act, (GoalConflict,), "不允许的Goal状态转移")

    # P02 追加：未到期转 expired 必须 409
    def expired_early():
        env2 = Env(os.path.join(tmp, "early.db"))
        goal = env2.authorize(env2.create())
        goal = env2.step(goal, "ready")
        return env2.step(goal, "expired")
    blocked("P02f 未到期转expired被拒绝", expired_early, (GoalConflict,), "尚未过期")

    # P03 跨连接 CAS
    db2 = Database(os.path.join(tmp, "goals.db"))
    store2 = GoalStore(db2, clock=lambda: env.clock[0])
    base = drive(env, "ready")
    results, barrier = [], threading.Barrier(2)

    def race(active):
        barrier.wait()
        try:
            active.transition(base["goal_id"], "running", actor="racer",
                              reason="cas", expected_revision=base["revision"])
            results.append("ok")
        except GoalConflict:
            results.append("conflict")

    t1 = threading.Thread(target=race, args=(store,))
    t2 = threading.Thread(target=race, args=(store2,))
    t1.start(); t2.start(); t1.join(); t2.join()
    RESULTS.append((sorted(results) == ["conflict", "ok"],
                    "P03 跨连接CAS恰好一个赢家", str(results)))

    # P04 重启持久化
    goal = drive(env, "ready")
    gid, rev = goal["goal_id"], goal["revision"]
    env.db.close()
    reopened = Database(os.path.join(tmp, "goals.db"))
    store3 = GoalStore(reopened, clock=lambda: 1_000_000.0)
    survived = ok("P04a 重启后Goal仍可读", lambda: store3.get(gid))
    if survived is not None:
        RESULTS.append((survived["status"] == "ready" and survived["revision"] == rev,
                        "P04b 重启后状态与版本保持", str((survived["status"], survived["revision"]))))
        RESULTS.append((len(store3.evidence(gid)) > 0, "P04c 重启后证据链保留",
                        str(len(store3.evidence(gid)))))
        ok("P04d 重启后expire_due可运行", lambda: store3.expire_due())
    reopened.close()

    # P05 幂等与指纹
    env = Env(os.path.join(tmp, "idem.db"))
    store = env.store
    g1 = ok("P05a command_id创建首次", lambda: env.create(command_id="cmd-create-1"))
    g2 = ok("P05b 同键同载荷重放", lambda: env.create(command_id="cmd-create-1"))
    RESULTS.append((g1 is not None and g2 is not None and g1["goal_id"] == g2["goal_id"],
                    "P05c 重放返回同一Goal", ""))
    blocked("P05d 同键改载荷被拒绝",
            lambda: env.create(title="other", command_id="cmd-create-1"),
            (GoalConflict,), "幂等键与原命令不匹配")

    goal2 = env.authorize(env.create(title="idem2"))
    t1r = ok("P05e 带command_id转移首次", lambda: env.store.transition(
        goal2["goal_id"], "ready", actor="user", reason="step",
        expected_revision=goal2["revision"], command_id="cmd-ready-2"))
    count_before = len(env.store.evidence(goal2["goal_id"]))
    t2r = ok("P05f 带command_id转移重放", lambda: env.store.transition(
        goal2["goal_id"], "ready", actor="user", reason="step",
        expected_revision=goal2["revision"], command_id="cmd-ready-2"))
    count_after = len(env.store.evidence(goal2["goal_id"]))
    RESULTS.append((t1r is not None and t2r is not None and
                    t1r["revision"] == t2r["revision"] and count_before == count_after,
                    "P05g 重放不重复加版本/证据", f"{count_before}->{count_after}"))

    # P06 授权期限 / 撤销 / 替换
    env = Env(os.path.join(tmp, "auth.db"))
    store = env.store

    def ready_goal(env, title="purpose", **kw):
        goal = env.authorize(env.create(title=title, **kw))
        return env.step(goal, "ready")

    goal = ready_goal(env)
    task = Task(user_goal="t", goal_id=goal["goal_id"])
    ok("P06a 有效授权可绑任务", lambda: store.bind_task(task, "execute"))

    g_exp = ready_goal(env, title="exp", expires_at=env.clock[0] + 100) if False else None
    g_exp = env.authorize(env.create(title="exp"), expires_at=env.clock[0] + 100)
    g_exp = env.step(g_exp, "ready")
    t_exp = Task(user_goal="t2", goal_id=g_exp["goal_id"])
    ok("P06b 过期前可绑任务", lambda: store.bind_task(t_exp, "execute"))
    env.clock[0] += 200
    blocked("P06c 授权过期后不得新建任务",
            lambda: store.bind_task(Task(user_goal="t2b", goal_id=g_exp["goal_id"]), "execute"),
            contains="授权失效")
    blocked("P06d 授权过期后check_task拒绝",
            lambda: store.check_task(t_exp.task_id), contains="授权")

    revoked_goal = ready_goal(env, title="rev")
    rev_task = Task(user_goal="t3", goal_id=revoked_goal["goal_id"])
    store.bind_task(rev_task, "execute")
    live = store.get(revoked_goal["goal_id"])
    store.revoke(revoked_goal["goal_id"], actor="admin", reason="revoke",
                 expected_revision=live["revision"])
    blocked("P06e 撤销后新任务被拒绝",
            lambda: store.bind_task(Task(user_goal="t4", goal_id=revoked_goal["goal_id"]), "execute"),
            contains="暂停或已终止")
    blocked("P06f 撤销后在途任务被拒绝",
            lambda: store.check_task(rev_task.task_id), contains="暂停或已终止")

    rep_goal = ready_goal(env, title="rep")
    rep_task = Task(user_goal="t5", goal_id=rep_goal["goal_id"])
    store.bind_task(rep_task, "execute")
    stale = store.get(rep_goal["goal_id"])
    rep_goal = env.authorize(stale, policy={"tools": ["read"], "permissions": []})
    blocked("P06g 重新授权后旧任务不得继续",
            lambda: store.check_task(rep_task.task_id), contains="授权版本已变更")
    blocked("P06h 旧版本重授权被拒绝",
            lambda: env.authorize(stale, policy={"tools": ["read"], "permissions": []}),
            (GoalConflict,), "版本已变更")

    # P07 父子边界九类扩张
    env = Env(os.path.join(tmp, "tree.db"))
    store = env.store
    root = tempfile.mkdtemp(prefix="aln13root_")
    parent_policy = {"tools": ["read", "write"], "permissions": ["p"],
                     "max_risk": "high", "targets": ["t1"], "device_ids": ["d1"],
                     "path_roots": [root]}
    parent = env.authorize(env.create(title="parent"), policy=parent_policy,
                           budget={"max_tool_calls": 5})
    child = env.create(title="child", parent_goal_id=parent["goal_id"])

    expansions = [
        ("tools", dict(policy=dict(parent_policy, tools=["read", "write", "exec"]))),
        ("permissions", dict(policy=dict(parent_policy, permissions=["p", "q"]))),
        ("risk", dict(policy=dict(parent_policy, max_risk="critical"))),
        ("targets", dict(policy=dict(parent_policy, targets=["t1", "t2"]))),
        ("device_ids", dict(policy=dict(parent_policy, device_ids=["d2"]))),
        ("path_roots", dict(policy=dict(parent_policy,
                                        path_roots=[os.path.join(root, "..")]))),
        ("level", dict(level="continuous", policy=parent_policy)),
        ("expiry", dict(policy=parent_policy, expires_at=env.clock[0] + 6000)),
        ("budget", dict(policy=parent_policy, budget={"max_tool_calls": 6})),
    ]
    for label, kwargs in expansions:
        def act(kwargs=kwargs, label=label):
            fresh = env.create(title="c-" + label, parent_goal_id=parent["goal_id"])
            return store.authorize(fresh["goal_id"], actor="admin", reason="grant",
                                   expected_revision=fresh["revision"],
                                   level=kwargs.get("level", "execution"),
                                   expires_at=kwargs.get("expires_at", env.clock[0] + 4000),
                                   budget_policy=kwargs.get("budget"),
                                   policy=kwargs["policy"])
        blocked(f"P07 子目标扩张{label}被拒绝", act, contains="子目标")

    narrow = env.authorize(child, level="preparation",
                           policy={"tools": ["read"], "permissions": [],
                                   "max_risk": "low", "targets": ["t1"],
                                   "device_ids": ["d1"], "path_roots": [root]},
                           budget={"max_tool_calls": 3})
    RESULTS.append((narrow["revision"] == child["revision"] + 1,
                    "P07j 子目标收窄范围获准", ""))
    blocked("P07k 子目标期限越过父期限被拒绝",
            lambda: env.create(title="late", parent_goal_id=parent["goal_id"],
                               deadline=parent["deadline"] + 1),
            contains="期限超过父目标")
    blocked("P07l 派生Goal缺父目标被拒绝",
            lambda: env.create(title="orphan", goal_type="derived"),
            (ValueError,), "必须关联父目标")
    terminal = env.create(title="term", parent_goal_id=parent["goal_id"])
    terminal = env.step(terminal, "cancelled")
    blocked("P07m 终态父目标下不得新建子Goal",
            lambda: env.create(title="under-dead", parent_goal_id=terminal["goal_id"]),
            contains="父目标已终止")

    # P08 五级动作矩阵
    env = Env(os.path.join(tmp, "levels.db"))
    store = env.store
    made = {}
    for level in ("suggestion", "preparation", "sandbox", "execution", "continuous"):
        goal = env.authorize(env.create(title=level), level=level)
        made[level] = env.step(goal, "ready")

    blocked("P08a suggestion不得创建任务",
            lambda: store.bind_task(Task(user_goal="s", goal_id=made["suggestion"]["goal_id"]),
                                    "suggest"), contains="建议")
    blocked("P08b suggestion不得prepare",
            lambda: store.bind_task(Task(user_goal="s2", goal_id=made["suggestion"]["goal_id"]),
                                    "prepare"), contains="超出自主等级")
    prep_task = ok("P08c preparation可建prepare任务",
                   lambda: store.bind_task(Task(user_goal="p", goal_id=made["preparation"]["goal_id"]),
                                           "prepare"))
    if prep_task is not None:
        blocked("P08d preparation禁止副作用工具",
                lambda: store.check_task(prep_task.task_id, CONTRACT_LOCAL, {}),
                contains="准备等级禁止实际副作用")
        ok("P08e preparation允许只读工具",
           lambda: store.check_task(prep_task.task_id, CONTRACT_NONE, {}))
    blocked("P08f preparation不得execute",
            lambda: store.bind_task(Task(user_goal="p2", goal_id=made["preparation"]["goal_id"]),
                                    "execute"), contains="超出自主等级")

    from evoagent.evolution.sandbox import SandboxRunner
    runner = SandboxRunner()
    blocked("P08g preparation不得调用沙箱",
            lambda: store.sandbox(made["preparation"]["goal_id"], "print(1)", runner, actor="admin"),
            contains="超出自主等级")
    ok("P08h sandbox可调用受控沙箱",
       lambda: store.sandbox(made["sandbox"]["goal_id"], "print(1)", runner, actor="admin"))
    ok("P08i execution可execute",
       lambda: store.bind_task(Task(user_goal="e", goal_id=made["execution"]["goal_id"]), "execute"))
    blocked("P08j execution不得advance",
            lambda: store.bind_task(Task(user_goal="e2", goal_id=made["execution"]["goal_id"]),
                                    "advance"), contains="超出自主等级")
    ok("P08k continuous可advance",
       lambda: store.bind_task(Task(user_goal="c", goal_id=made["continuous"]["goal_id"]), "advance"))

    # P09 任务预算收紧与最终动作复核
    env = Env(os.path.join(tmp, "task.db"))
    store = env.store
    goal = env.authorize(env.create(title="bud"),
                         budget={"max_tool_calls": 5, "max_seconds": 60})
    goal = env.step(goal, "ready")
    task = Task(user_goal="b", goal_id=goal["goal_id"], budget_policy={"max_tool_calls": 9})
    bound = ok("P09a 超额预算任务可提交", lambda: store.bind_task(task, "execute"))
    if bound is not None:
        RESULTS.append((bound.budget_policy.get("max_tool_calls") == 5
                        and bound.budget_policy.get("max_seconds") == 60,
                        "P09b 任务预算被收紧到Goal上限", str(bound.budget_policy)))
        RESULTS.append((bound.runtime_manifest.get("goal", {}).get("goal_id") == goal["goal_id"],
                        "P09c runtime_manifest保留goal引用", ""))
    blocked("P09d 未知预算字段被拒绝",
            lambda: store.bind_task(Task(user_goal="u", goal_id=goal["goal_id"],
                                         budget_policy={"tool_limits": {}}), "execute"),
            (ValueError,), "未知或嵌套预算")

    live = store.get(goal["goal_id"])  # bind_task 可能把 ready 转 running
    task2 = Task(user_goal="g", goal_id=live["goal_id"])
    store.bind_task(task2, "execute")
    ok("P09e 最终动作复核通过", lambda: store.check_task(task2.task_id, CONTRACT_NONE, {}))
    live = store.get(live["goal_id"])
    store.transition(live["goal_id"], "paused", actor="user", reason="pause",
                     expected_revision=live["revision"])
    blocked("P09f 目标暂停后最终动作被拒绝",
            lambda: store.check_task(task2.task_id, CONTRACT_NONE, {}), contains="暂停")
    live = store.get(live["goal_id"])
    store.transition(live["goal_id"], "cancelled", actor="user", reason="cancel",
                     expected_revision=live["revision"])
    blocked("P09g 目标取消后最终动作被拒绝",
            lambda: store.check_task(task2.task_id, CONTRACT_NONE, {}), contains="终止")

    # P10 未注册占位Goal / 自主来源缺绑定
    legacy = Task(user_goal="legacy", goal_id="goal_missing_0001")
    env.db.save_task(legacy)
    blocked("P10a 未注册Goal引用不能继续执行",
            lambda: store.check_task(legacy.task_id), contains="绑定")
    auto = Task(user_goal="auto", origin="derived")
    env.db.save_task(auto)
    blocked("P10b 自主来源任务缺少Goal绑定被拒绝",
            lambda: store.check_task(auto.task_id), contains="缺少Goal授权绑定")

    # P11 API 角色与 actor 不可伪造
    import evoagent.api.goals_api as api

    class Ctx:
        def __init__(self, role, body):
            self.auth = type("A", (), {"role": role})()
            self.body = body
            self.app = type("App", (), {"goals": store})()
            self.path = "/goals"
            self.query = {}
            self.response = None

        def json(self, code, payload):
            self.response = (code, payload)
            return self.response

    user_ctx = Ctx("user", {"title": "x", "success_criteria": ["y"], "deadline": 2_000_000})
    api.create_goal(user_ctx)
    RESULTS.append((user_ctx.response is not None and user_ctx.response[0] == 403,
                    "P11a 非admin写操作403", str(user_ctx.response)))
    spoof = Ctx("admin", {"title": "spoof", "success_criteria": ["y"],
                          "deadline": 2_000_000, "actor": "attacker"})
    api.create_goal(spoof)
    RESULTS.append((spoof.response is not None and spoof.response[0] == 400,
                    "P11b body伪造actor被拒绝", str(spoof.response)[:120]))
    good = Ctx("admin", {"title": "api-goal", "success_criteria": ["y"],
                         "deadline": 2_000_000})
    api.create_goal(good)
    RESULTS.append((good.response is not None and good.response[0] == 201
                    and good.response[1].get("owner") == "api:admin",
                    "P11c actor取自传输鉴权", str(good.response)[:120]))

    # P12 撤销不声称撤回已发副作用（静态检查）
    src = inspect.getsource(GoalStore.revoke)
    RESULTS.append((not any(word in src.lower()
                            for word in ("undo", "compensat", "rollback", "restore")),
                    "P12 revoke仅阻止新动作，无撤回副作用代码", ""))

    passed = sum(1 for ok_, _, _ in RESULTS if ok_)
    print(f"ALN-13-A 独立探针: {passed}/{len(RESULTS)} passed")
    for ok_, name, detail in RESULTS:
        if not ok_:
            print(f"FAIL {name} :: {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
