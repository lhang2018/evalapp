"""ALN-13-A 独立复核探针（claude-main 自写，不引用作者 150 项测试）。

覆盖作者声明里最硬的不变量：终态封闭、跨连接 CAS、父子扩权拒绝、
授权期限约束、撤销生效、命令幂等，外加一条成功路径护栏。
"""
from __future__ import annotations

import os
import sys
import tempfile

RESULTS: list = []


def probe(name, fn):
    try:
        fn()
        RESULTS.append((name, True, ""))
        print(f"[PASS] {name}")
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")


def _db_path(tag):
    return os.path.join(tempfile.mkdtemp(prefix=f"aln13-{tag}-"), "g.db")


def _store(path=None, *, clock=None):
    from evoagent.core.persistence import Database
    from evoagent.goals import GoalStore
    db = Database(path or _db_path("s"))
    return GoalStore(db, clock=clock), db


def _policy(**over):
    base = {"tools": ["file_write"], "permissions": [],
            "path_roots": ["/tmp/root"], "targets": [], "device_ids": [],
            "max_risk": "low"}
    base.update(over)
    return base


def _goal(store, title="复核目标", **kw):
    kw.setdefault("deadline", store.clock() + 3600)
    return store.create(title, actor="user", success_criteria=["ok"], **kw)


def _authorize(store, goal, level="suggestion", **over):
    """按契约先授权再激活：_grants 要求 authorization_ref 存在。"""
    goal = store.get(goal["goal_id"])
    sequential = min(store.clock() + 600, goal["deadline"] - 1)
    return store.authorize(goal["goal_id"], _policy(**over), level=level,
                           actor="admin", reason="复核授权",
                           expected_revision=goal["revision"],
                           expires_at=sequential)


def _ready_with(store, goal, **over):
    """需要父范围约束的子目标：先以合规范围授权再激活。"""
    _authorize(store, goal, "suggestion", **over)
    current = store.get(goal["goal_id"])
    return store.transition(goal["goal_id"], "ready", actor="user",
                            reason="activate",
                            expected_revision=current["revision"])


def _ready(store, goal):
    _authorize(store, goal, "suggestion")
    current = store.get(goal["goal_id"])
    return store.transition(goal["goal_id"], "ready", actor="user",
                            reason="activate",
                            expected_revision=current["revision"])


# ---------- G1 终态封闭 ----------

def g1_terminal_state_is_closed():
    store, db = _store()
    try:
        goal = _goal(store)
        store.transition(goal["goal_id"], "cancelled", actor="user",
                         reason="用户取消", expected_revision=goal["revision"])
        current = store.get(goal["goal_id"])
        assert current["status"] == "cancelled"
        for target in ("ready", "running", "completed", "paused"):
            try:
                store.transition(goal["goal_id"], target, actor="user",
                                 reason="复活尝试",
                                 expected_revision=current["revision"])
            except Exception:
                continue
            raise AssertionError(f"终态目标被转移成 {target}（终态未封闭）")
    finally:
        db.close()


# ---------- G2 跨连接 CAS ----------

def g2_cross_connection_cas_conflict():
    path = _db_path("cas")
    s1, db1 = _store(path)
    try:
        goal = _goal(s1)
        _ready(s1, goal)                  # 先满足「未授权不得激活」契约
        s2, db2 = _store(path)
        try:
            same = s2.get(goal["goal_id"])
            assert same["revision"] == s1.get(goal["goal_id"])["revision"], \
                "两连接读到不同版本"
            s1.transition(goal["goal_id"], "running", actor="admin",
                          reason="先手提交", expected_revision=same["revision"])
            try:
                s2.transition(goal["goal_id"], "paused", actor="admin",
                              reason="后手用旧版本",
                              expected_revision=same["revision"])
            except Exception:
                return
            raise AssertionError(
                "另一连接用过期 revision 改状态成功——CAS 未跨连接生效")
        finally:
            db2.close()
    finally:
        db1.close()


# ---------- G3/G4 父子扩权拒绝 ----------

def g3_child_cannot_expand_path_roots():
    store, db = _store()
    try:
        parent = _goal(store, "父目标")
        _ready(store, parent)
        parent = store.get(parent["goal_id"])
        store.authorize(parent["goal_id"], _policy(path_roots=["/tmp/parent"]),
                        level="execution", actor="admin", reason="父授权",
                        expected_revision=parent["revision"],
                        expires_at=store.clock() + 1800)
        parent = store.get(parent["goal_id"])
        child = _goal(store, "子目标", goal_type="derived",
                      parent_goal_id=parent["goal_id"],
                      deadline=parent["deadline"] - 60)   # 不得超父期限
        _ready_with(store, child, path_roots=["/tmp/parent"])
        child = store.get(child["goal_id"])
        try:
            store.authorize(child["goal_id"],
                            _policy(path_roots=["/tmp/parent", "/etc"]),
                            level="execution", actor="admin", reason="扩权",
                            expected_revision=child["revision"],
                            expires_at=store.clock() + 600)
        except Exception:
            return
        raise AssertionError("子目标把文件根扩到了父范围之外（扩权未被拒绝）")
    finally:
        db.close()


def g4_child_deadline_cannot_exceed_parent():
    store, db = _store()
    try:
        parent = _goal(store, "父目标", deadline=store.clock() + 600)
        try:
            _goal(store, "子目标", goal_type="derived",
                  parent_goal_id=parent["goal_id"],
                  deadline=store.clock() + 99999)
        except Exception:
            return
        raise AssertionError("子目标期限超过父目标却被接受")
    finally:
        db.close()


# ---------- G5 授权期限与撤销 ----------

def g5_authorization_must_fit_goal_deadline():
    store, db = _store()
    try:
        goal = _goal(store, deadline=store.clock() + 600)
        _ready(store, goal)
        goal = store.get(goal["goal_id"])
        try:
            store.authorize(goal["goal_id"], _policy(), level="execution",
                            actor="admin", reason="超期授权",
                            expected_revision=goal["revision"],
                            expires_at=store.clock() + 99999)
        except Exception:
            return
        raise AssertionError("授权期限超出目标期限却被接受")
    finally:
        db.close()


def g6_revocation_blocks_and_clears_grants():
    store, db = _store()
    try:
        goal = _goal(store)
        _ready(store, goal)
        goal = store.get(goal["goal_id"])
        store.authorize(goal["goal_id"], _policy(), level="execution",
                        actor="admin", reason="授权",
                        expected_revision=goal["revision"],
                        expires_at=store.clock() + 600)
        goal = store.get(goal["goal_id"])
        assert goal["status"] == "ready"
        store.revoke(goal["goal_id"], actor="admin", reason="紧急撤销",
                     expected_revision=goal["revision"])
        after = store.get(goal["goal_id"])
        assert after["status"] == "blocked", (
            f"撤销后状态为 {after['status']}，未阻断后续动作")
        # 撤销后按 active 查询授权：要么被拒绝，要么返回空——都是正确拦截
        try:
            _, active = store._grants(goal["goal_id"], "execute", active=True)
        except PermissionError:
            return
        assert not active, "撤销后仍有生效授权"
    finally:
        db.close()


# ---------- G7 命令幂等 ----------

def g7_duplicate_command_returns_same_goal():
    store, db = _store()
    try:
        deadline = store.clock() + 600      # 同命令必须完全一致，否则指纹不同
        first = store.create("幂等目标", actor="user", success_criteria=["ok"],
                             deadline=deadline, command_id="cmd-same")
        second = store.create("幂等目标", actor="user", success_criteria=["ok"],
                              deadline=deadline, command_id="cmd-same")
        assert first["goal_id"] == second["goal_id"], (
            f"同一 command_id 产生了两个目标: {first['goal_id']} vs "
            f"{second['goal_id']}")
    finally:
        db.close()


def g7b_different_command_creates_new_goal():
    """护栏：不同 command_id 必须产生不同目标（幂等不能过度收敛）。"""
    store, db = _store()
    try:
        a = store.create("A", actor="user", success_criteria=["ok"],
                         deadline=store.clock() + 600, command_id="cmd-a")
        b = store.create("B", actor="user", success_criteria=["ok"],
                         deadline=store.clock() + 600, command_id="cmd-b")
        assert a["goal_id"] != b["goal_id"], "不同命令被错误合并成同一目标"
    finally:
        db.close()


# ---------- G8 成功路径护栏 ----------

def g8_happy_path_still_works():
    store, db = _store()
    try:
        goal = _goal(store)
        assert goal["status"] == "proposed"
        _ready(store, goal)
        goal = store.get(goal["goal_id"])
        assert goal["status"] == "ready"
        store.authorize(goal["goal_id"], _policy(), level="execution",
                        actor="admin", reason="授权",
                        expected_revision=goal["revision"],
                        expires_at=store.clock() + 600)
        goal = store.get(goal["goal_id"])
        assert goal["authorization_ref"], "合法授权未写入引用"
        store.transition(goal["goal_id"], "running", actor="admin",
                         reason="开始", expected_revision=goal["revision"])
        assert store.get(goal["goal_id"])["status"] == "running"
    finally:
        db.close()


if __name__ == "__main__":
    probe("G1 terminal_state_is_closed", g1_terminal_state_is_closed)
    probe("G2 cross_connection_cas_conflict", g2_cross_connection_cas_conflict)
    probe("G3 child_cannot_expand_path_roots", g3_child_cannot_expand_path_roots)
    probe("G4 child_deadline_cannot_exceed_parent",
          g4_child_deadline_cannot_exceed_parent)
    probe("G5 authorization_must_fit_goal_deadline",
          g5_authorization_must_fit_goal_deadline)
    probe("G6 revocation_blocks_and_clears_grants",
          g6_revocation_blocks_and_clears_grants)
    probe("G7 duplicate_command_same_goal", g7_duplicate_command_returns_same_goal)
    probe("G7b different_command_new_goal (护栏)",
          g7b_different_command_creates_new_goal)
    probe("G8 happy_path_still_works (护栏)", g8_happy_path_still_works)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\ntotal={len(RESULTS)} passed={passed} failed={len(RESULTS) - passed}")
    sys.exit(0 if passed == len(RESULTS) else 1)
