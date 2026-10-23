"""ALN-14-A 机会排序与目标治理独立探针（claude-main 第三方复核）。

从治理契约行为出发独立构造，不复用被审方 42 项测试的断言：

O1 排序确定性：同分候选按 candidate_id 稳定排序；候选顺序不影响结果；
O2 未知费用/证据不排名（needs_evidence），过期排除（expired）；
O3 评估数值防线：confidence>1、非法 risk、超范围数值拒绝；
O4 策略不可变：同 ref 不同 rules 拒绝；同 ref 同 rules 幂等（护栏）；
O5 discover 幂等：同 source_ref 二次发现返回同一候选（不重复建）；
O6 默认策略存在：manual-review/v1 可被 ranked 直接使用（护栏）。
"""
import sys
import os
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def probe(name, fn):
    try:
        fn()
        results.append((name, "PASS"))
    except (AssertionError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        results.append((name, f"FAIL: {type(exc).__name__}: {exc}"))


def new_svc():
    from evoagent.core.persistence import Database
    from evoagent.core.task import Task, TaskStatus
    from evoagent.goals.governance import GoalGovernor
    from evoagent.goals.opportunities import OpportunityService
    tmp = tempfile.mkdtemp(prefix="opp-probe-")
    db = Database(os.path.join(tmp, "g.db"))
    clock = [time.time()]
    svc = OpportunityService(db, clock=lambda: clock[0])
    gov = GoalGovernor(db, clock=lambda: clock[0])
    made = [0]

    def make_terminal_task():
        """建一个真实终态 Task 作为机会来源（discover 的硬要求）。"""
        made[0] += 1
        t = Task(user_goal=f"probe goal {made[0]}",
                 task_status=TaskStatus.SUCCEEDED)
        db.save_task(t)
        return t.task_id
    return svc, gov, db, clock, make_terminal_task


def discovered(svc, n, *, source_kind="task", _mk=None, **kw):
    mk = _mk or discovered._mk
    sid = mk() if source_kind == "task" else f"src-{n}"
    return svc.discover(source_kind, sid, actor="admin",
                        reason="复核探针", expires_at=time.time() + 3600, **kw)


# ---- O1 排序确定性 ----
def o1_ranking_deterministic():
    svc, gov, db, clock, mk_t = new_svc()
    discovered._mk = mk_t
    a = discovered(svc, 1)
    b = discovered(svc, 2)
    svc.assess(a["candidate_id"], expected_benefit=10, evidence_confidence=0.5,
               estimated_cost=1.0, opportunity_cost=0.0, risk="low",
               actor="admin", reason="r", expected_revision=a["revision"])
    svc.assess(b["candidate_id"], expected_benefit=10, evidence_confidence=0.5,
               estimated_cost=1.0, opportunity_cost=0.0, risk="low",
               actor="admin", reason="r", expected_revision=b["revision"])
    r1 = [c["candidate_id"] for c in svc.ranked()]
    r2 = [c["candidate_id"] for c in svc.ranked()]
    assert r1 == r2 and len(r1) == 2, "排序不稳定"
    scores = {c["candidate_id"]: c["score"] for c in svc.ranked()}
    assert scores[a["candidate_id"]] == scores[b["candidate_id"]] != None
    # 同分 → candidate_id 字典序
    first = r1[0]
    assert first == min(a["candidate_id"], b["candidate_id"]), (
        f"同分未按 candidate_id 稳定排序: {r1}")


# ---- O2 未知费用不排名、过期排除 ----
def o2_unknown_cost_and_expired():
    svc, gov, db, clock, mk_t = new_svc()
    discovered._mk = mk_t
    unk = discovered(svc, 10)
    svc.assess(unk["candidate_id"], expected_benefit=50,
               evidence_confidence=0.9, estimated_cost=None,
               opportunity_cost=0.0, risk="low",
               actor="admin", reason="r", expected_revision=unk["revision"])
    exp = discovered(svc, 11)
    svc.assess(exp["candidate_id"], expected_benefit=90,
               evidence_confidence=0.9, estimated_cost=1.0,
               opportunity_cost=0.0, risk="low",
               actor="admin", reason="r", expected_revision=exp["revision"])
    clock[0] += 7200  # 全部过期
    rows = {c["candidate_id"]: c for c in svc.ranked()}
    assert rows[unk["candidate_id"]]["ranking_state"] == "expired"
    assert rows[unk["candidate_id"]]["score"] is None
    assert rows[exp["candidate_id"]]["score"] is None


def o2b_unknown_cost_before_expiry_needs_evidence():
    svc, gov, db, clock, mk_t = new_svc()
    discovered._mk = mk_t
    unk = discovered(svc, 20)
    svc.assess(unk["candidate_id"], expected_benefit=50,
               evidence_confidence=0.9, estimated_cost=None,
               opportunity_cost=0.0, risk="low",
               actor="admin", reason="r", expected_revision=unk["revision"])
    rows = {c["candidate_id"]: c for c in svc.ranked()}
    assert rows[unk["candidate_id"]]["ranking_state"] == "needs_evidence"
    assert rows[unk["candidate_id"]]["score"] is None


# ---- O3 数值防线 ----
def o3_invalid_assessment_rejected():
    svc, gov, db, clock, mk_t = new_svc()
    discovered._mk = mk_t
    c = discovered(svc, 30)
    bad = [
        dict(evidence_confidence=1.5),
        dict(risk="yolo"),
        dict(expected_benefit=10**12),
    ]
    base = dict(expected_benefit=10, evidence_confidence=0.5,
                estimated_cost=1.0, opportunity_cost=0.0, risk="low")
    for extra in bad:
        kw = dict(base)
        kw.update(extra)
        try:
            svc.assess(c["candidate_id"], actor="admin", reason="r",
                       expected_revision=c["revision"], **kw)
        except (ValueError, KeyError):
            continue
        raise AssertionError(f"非法评估未被拒绝: {extra}")


# ---- O4 策略不可变 ----
def o4_policy_immutable():
    from evoagent.goals.governance import GoalGovernor
    svc, gov, db, clock, mk_t = new_svc()
    discovered._mk = mk_t
    gov = GoalGovernor(db, clock=lambda: clock[0])
    rules = {"cost_weight": 2.0, "risk_weight": 1.0}
    gov.install_policy("probe/v1", rules, actor="admin", reason="安装")
    try:
        gov.install_policy("probe/v1", {"cost_weight": 9.9, "risk_weight": 1.0},
                           actor="admin", reason="改写")
    except Exception:
        pass
    else:
        raise AssertionError("策略版本被重写（应不可变）")
    # 护栏：同 rules 幂等
    again = gov.install_policy("probe/v1", rules, actor="admin", reason="重复")
    assert again["rules"]["cost_weight"] == 2.0


def o4b_default_policy_usable():
    svc, gov, db, clock, mk_t = new_svc()
    discovered._mk = mk_t
    c = discovered(svc, 40)
    svc.assess(c["candidate_id"], expected_benefit=8, evidence_confidence=0.7,
               estimated_cost=0.5, opportunity_cost=0.0, risk="low",
               actor="admin", reason="r", expected_revision=c["revision"])
    rows = svc.ranked()
    assert rows and rows[0]["candidate_id"] == c["candidate_id"]
    assert rows[0]["ranking_state"] == "ranked"
    assert rows[0]["ranking_policy_ref"] == "manual-review/v1"


# ---- O5 discover 幂等 ----
def o5_discover_idempotent():
    svc, gov, db, clock, mk_t = new_svc()
    discovered._mk = mk_t
    sid = mk_t()
    a = svc.discover("task", sid, actor="admin", reason="r",
                     expires_at=time.time() + 3600)
    b = svc.discover("task", sid, actor="admin", reason="r",
                     expires_at=time.time() + 3600)
    assert a["candidate_id"] == b["candidate_id"], "同源重复发现建了新候选"


probe("O1 ranking-deterministic", o1_ranking_deterministic)
probe("O2 expired-and-unknown-excluded", o2_unknown_cost_and_expired)
probe("O2b unknown-cost-needs-evidence", o2b_unknown_cost_before_expiry_needs_evidence)
probe("O3 invalid-assessment-rejected", o3_invalid_assessment_rejected)
probe("O4 policy-immutable", o4_policy_immutable)
probe("O4b default-policy-usable[guard]", o4b_default_policy_usable)
probe("O5 discover-idempotent", o5_discover_idempotent)

print("=" * 56)
for name, status in results:
    print(f"{status:8} {name}")
failed = [n for n, s in results if s.startswith("FAIL")]
print("=" * 56)
print(f"total={len(results)} pass={len(results)-len(failed)} fail={len(failed)}")
sys.exit(1 if failed else 0)
