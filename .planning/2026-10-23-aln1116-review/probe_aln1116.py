"""ALN-11-R / ALN-16-R 独立验收探针（reviewer: claude-main，2026-10-23）。

按 cwd 加载被审源码：在干净 worktree 内执行本脚本。
不复用被审方 tests/。覆盖 codex-runtime 修复的 CR16-01..06 六个缺陷：
每个探针断言「修复后的正确行为」，因此在修复前提交(03600e1)应失败、在修复提交(aae0f9b)应通过。
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.getcwd())  # 导入 cwd（被审 checkout）的源码

from evoagent.api.auth import ApiAuth  # noqa: E402
from evoagent.observability.chain import build_decision_chain  # noqa: E402
from evoagent.observability.contract import (  # noqa: E402
    EvidenceContractError, EvidenceEvent, as_evidence_event)
from evoagent.observability.credentials import (  # noqa: E402
    CredentialError, CredentialRegistry, TokenRing)

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + ((" | " + detail) if detail else ""))


def run_event(**kw):
    base = dict(event_id="ev1", event_type="tool_result", timestamp=100.0, payload={},
                origin="server", trust="trusted", privacy_level="internal",
                fault_level="none", severity="info", component_id="c",
                component_version="1", refs={}, payload_ref=None,
                correlation_id=None, causation_id=None, sequence_no=1,
                untrusted_origin="", redacted=False)
    base.update(kw)
    return SimpleNamespace(**base)


def event_dict(**kw):
    base = dict(event_id="e1", event_type="tool_result", timestamp=10.0,
                refs={"task_id": "T1", "run_id": "R1"}, sequence_no=1, payload={})
    base.update(kw)
    return base


# ---------- CR16-01：RunEvent 转换必须执行同一校验 ----------
try:
    EvidenceEvent.from_run_event(run_event(trust="untrusted", untrusted_origin=""))
    rejected = False
except EvidenceContractError:
    rejected = True
check("CR16-01 RunEvent 转换拒绝无来源的 untrusted 事件", rejected)

try:
    EvidenceEvent.from_run_event(run_event(privacy_level="bogus"))
    rejected = False
except EvidenceContractError:
    rejected = True
check("CR16-01b RunEvent 转换拒绝非法隐私等级", rejected)

try:
    as_evidence_event({"event_id": "x", "trust": "untrusted", "untrusted_origin": ""})
    rejected = False
except EvidenceContractError:
    rejected = True
check("CR16-01c dict 路径校验未被削弱（回归）", rejected)

# ---------- CR16-02：默认 none 按严重度推断 ----------
ev = EvidenceEvent.from_run_event(run_event(severity="error"))
check("CR16-02 severity=error 推断为 degraded", ev.fault_level == "degraded",
      f"fault_level={ev.fault_level}")
ev2 = EvidenceEvent.from_run_event(run_event(severity="critical"))
check("CR16-02b severity=critical 推断为 fatal", ev2.fault_level == "fatal",
      f"fault_level={ev2.fault_level}")
ev3 = EvidenceEvent.from_run_event(run_event(severity="info", event_type="heartbeat"))
check("CR16-02c 普通事件仍为 none", ev3.fault_level == "none",
      f"fault_level={ev3.fault_level}")

# ---------- CR16-03：轮换语义 ----------
reg = CredentialRegistry()
reg.register("old", "api_token", secret="s1", not_after=1000.0)
try:
    reg.rotate("old", "new", secret=None, at=500.0)  # 非法新凭证 → 轮换必须失败
    failed = False
except CredentialError:
    failed = True
old = reg.get("old")
check("CR16-03a 轮换失败不得退役旧凭证", failed and old.status == "active"
      and old.not_after == 1000.0, f"failed={failed} status={old.status} not_after={old.not_after}")

reg2 = CredentialRegistry()
reg2.register("old2", "api_token", secret="s1", not_after=1000.0)
reg2.rotate("old2", "new2", secret="s2", grace_seconds=86400, at=500.0)
old2 = reg2.get("old2")
check("CR16-03b 宽限期不得延长原截止时间", old2.not_after == 1000.0,
      f"not_after={old2.not_after}")

reg3 = CredentialRegistry()
reg3.register("pub1", "client_trust", public_key="-----BEGIN PUBLIC KEY-----AAA")
try:
    rotated = reg3.rotate("pub1", "pub2", public_key="-----BEGIN PUBLIC KEY-----BBB",
                          at=500.0)
    ok = rotated.public_key.endswith("BBB") and reg3.get("pub1").status == "retired"
except Exception as exc:  # 修复前 rotate 不接受 public_key
    ok = False
    print("   rotate(public_key) raised:", type(exc).__name__, exc)
check("CR16-03c client_trust 公钥可轮换（无需私钥）", ok)

# ---------- CR16-04：撤销与复用 ----------
ring = TokenRing()
kid = ring.add("ring-secret-value", "operator")
ring.revoke(kid, reason="revoked by probe")
auth = ApiAuth(admin_token="ring-secret-value", token_ring=ring)
res = auth.check("Bearer ring-secret-value", "GET", "/tasks")
check("CR16-04a 已撤销令牌不得回退静态配置复活", (not res.ok) and res.status == 403,
      f"ok={res.ok} role={res.role} status={res.status}")

ring2 = TokenRing()
ring2.add("same-secret-value", "operator")
try:
    ring2.add("same-secret-value", "admin")
    rejected = False
except CredentialError:
    rejected = True
check("CR16-04b 同一秘密不得重复登记", rejected)

# ---------- CR16-05：任务/运行作用域隔离 ----------
events = [event_dict(event_id="e1", approval_id="A1", sequence_no=1),
          event_dict(event_id="e2", approval_id="A1", sequence_no=2)]
approvals = [{"approval_id": "A1", "task_id": "T1", "run_id": "R1", "status": "approved"},
             {"approval_id": "A9", "task_id": "T2", "run_id": "R9", "status": "approved"}]
chain = build_decision_chain(events, task_id="T1", run_id="R1", approvals=approvals)
node_ids = {n.node_id for n in chain.nodes}
edge_ids = {e.source for e in chain.edges} | {e.target for e in chain.edges}
check("CR16-05 其他任务的审批记录不得混入链", "A9" not in node_ids and "A9" not in edge_ids,
      f"nodes={sorted(node_ids)}")

# ---------- CR16-06：确定性追溯 ----------
c1 = build_decision_chain(events, task_id="T1", run_id="R1", approvals=approvals)
c2 = build_decision_chain(list(reversed(events)), task_id="T1", run_id="R1",
                           approvals=list(reversed(approvals)))
check("CR16-06a digest 与输入顺序无关", c1.digest() == c2.digest(),
      f"{c1.digest()[:12]} vs {c2.digest()[:12]}")

broken = build_decision_chain(
    [event_dict(event_id="e1", causation_id="missing-event")], task_id="T1")
check("CR16-06b 缺失因果引用必须报告",
      any(m.get("kind") == "event" and m.get("id") == "missing-event"
          for m in broken.missing_refs), str(broken.missing_refs))

failed = [name for name, ok in RESULTS if not ok]
print(f"\n==== {len(RESULTS) - len(failed)}/{len(RESULTS)} passed ====")
if failed:
    print("FAILED:", failed)
sys.exit(1 if failed else 0)
