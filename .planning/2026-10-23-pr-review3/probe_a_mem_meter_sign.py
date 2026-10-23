"""九卡修复第三方复核独立探针 A 组：PR-04 / PR-05 / PR-06（claude-main）。

复现审核报告（2026-10-23-其他作者待审卡独立审核.md）所述缺陷，
但不读对方测试、不复用对方探针，从行为契约独立构造：

PR-04 记忆（ALN-05-M，修复 7132180）：
  M1 已删除旧 ID 直接 write 回写 → 修复前复活，修复后拒绝；
  M2 派生引用（derived_from）指向已删记忆 → 修复前带缓存链复活，修复后拒绝；
  M3 带身份写入覆盖他人 memory_id 的内容 → 修复前静默覆盖，修复后拒绝；
  M4 重复内容写入 → 修复前返回新造但未落库的幽灵 ID，修复后返回实际条目。

PR-05 计量（ALN-08-M，修复 7132180）：
  U1 适配器响应未带 usage 时，MeteredModel 不得把上一次请求的 provider
     用量当本次实测记费 → 修复前复用旧 usage，修复后降级 estimate/unknown。

PR-06 签名（ALN-03-S，修复 7132180）：
  K1 相同 key_id 不同 secret → 修复前静默覆盖历史身份，修复后拒绝；
  K2 相同 key_id 相同 secret 重试 → 修复前后均幂等（护栏）。

判别力要求：修复前失败、修复后通过（K2 为双向护栏）。
"""
import sys
import tempfile
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def probe(name, fn):
    try:
        fn()
        results.append((name, "PASS"))
    except (AssertionError, ValueError, TypeError, RuntimeError) as exc:
        results.append((name, f"FAIL: {type(exc).__name__}: {exc}"))


# ---------------------------------------------------------------- PR-04
def new_store():
    from evoagent.memory.store import MemoryStore
    tmp = tempfile.mkdtemp(prefix="mem-probe-")
    return MemoryStore(os.path.join(tmp, "mem.db"))


def m1_deleted_id_cannot_be_rewritten():
    from evoagent.memory.store import MemoryItem
    s = new_store()
    item = MemoryItem(content="原始记忆", scope_key="t1")
    s.write(item)
    mid = item.memory_id
    assert s.delete(mid), "删除应成功"
    rewrite = MemoryItem(content="复活的篡改内容", scope_key="t1",
                         memory_id=mid)
    try:
        s.write(rewrite)
    except Exception:
        return
    # 若未拒绝：不得真的落库复活
    back = s.get(mid)
    assert back is None or "复活" not in (back.content or ""), (
        "已删除旧 ID 被直接回写复活")


def m2_derived_from_tombstone_rejected():
    from evoagent.memory.store import MemoryItem
    s = new_store()
    base = MemoryItem(content="基础记忆", scope_key="t1")
    s.write(base)
    s.delete(base.memory_id)
    derived = MemoryItem(content="派生记忆", scope_key="t1",
                         relations=[{"relation": "derived_from",
                                     "target": base.memory_id}])
    try:
        s.write(derived)
    except Exception:
        return
    back = s.get(derived.memory_id)
    assert back is None, "引用已删记忆的派生项被落库（墓碑未检查派生链）"


def m3_identity_conflict_rejected():
    from evoagent.memory.store import MemoryItem, MemoryAccessError
    from evoagent.memory.manifest import MemoryIdentity
    s = new_store()
    ident_a = MemoryIdentity(user_id="user-a", task_id="task-1")
    a = MemoryItem(content="A 的记忆", scope_key="task-1",
                   user_id="user-a", task_id="task-1")
    s.write(a, identity=ident_a)
    # 带他人身份写入同一 memory_id：须因身份不可见而拒绝
    ident_b = MemoryIdentity(user_id="user-b", task_id="task-1")
    forged = MemoryItem(content="B 伪造的内容", scope_key="task-1",
                        user_id="user-b", task_id="task-1",
                        memory_id=a.memory_id)
    try:
        s.write(forged, identity=ident_b)
    except MemoryAccessError:
        return
    except Exception:
        return
    back = s.get(a.memory_id)
    assert back is not None and back.user_id == "user-a", (
        "他人 memory_id 内容被换身份覆盖")


def m4_duplicate_write_returns_persisted_id():
    from evoagent.memory.store import MemoryItem
    s = new_store()
    a = MemoryItem(content="完全相同的内容", scope_key="t1", task_id="task-1")
    s.write(a)
    dup = MemoryItem(content="完全相同的内容", scope_key="t1", task_id="task-1")
    ret = s.write(dup)
    mid = getattr(ret, "memory_id", "") or (ret.get("memory_id")
        if isinstance(ret, dict) else "") or ""
    assert mid, "write 应返回含 memory_id 的结果"
    back = s.get(mid)
    assert back is not None, (
        f"重复写返回的 {mid} 是未落库的幽灵 ID（无法读取）")
    # 语义：重复内容应返回已持久化的原条目（或派生链接到原条目），而非虚构新 ID


# ---------------------------------------------------------------- PR-05
def u1_stale_provider_usage_not_rebilled():
    from evoagent.models.metering import MeteredModel
    from evoagent.models.base import ChatMessage

    class StaleAdapter:
        """第一次返回带 usage 的响应并设置 last_usage；
        第二次响应不带 usage、不清空 last_usage——修复前会复用旧 token。"""
        def __init__(self):
            self.last_usage = None
            self.calls = 0

        def profile(self):
            class P:
                model_id = "stale-1"
                supports_vision = False
            return P()

        def generate(self, messages):
            self.calls += 1
            if self.calls == 1:
                self.last_usage = {"input_tokens": 100, "output_tokens": 50,
                                   "total_tokens": 150}
            # 第二次：不更新 last_usage，也不清空
            return "ok"

    m = MeteredModel(StaleAdapter())
    msgs = [ChatMessage(role="user", content="hi")]
    m.generate(msgs)
    first = m.last_usage
    m.generate(msgs)
    second = m.last_usage
    assert first is not None and second is not None
    # 第二次请求没有新鲜 provider usage：token_source 不得标为 provider 实测
    from evoagent.models.metering import TOKEN_PROVIDER, COST_PROVIDER
    assert second.token_source != TOKEN_PROVIDER, (
        f"第二次请求复用了上一次的 provider 用量（{second.input_tokens}/"
        f"{second.output_tokens}）当本次实测")
    assert second.cost_source != COST_PROVIDER, (
        "第二次请求按上一次用量记 provider 费用")


# ---------------------------------------------------------------- PR-06
def _keyring():
    from evoagent.release.signing import KeyRing
    return KeyRing()


def k1_same_key_id_different_secret_rejected():
    kr = _keyring()
    kr.add("k1", "secret-A")
    try:
        kr.add("k1", "secret-B")
    except Exception:
        return
    key = kr.get("k1") if hasattr(kr, "get") else None
    secret = getattr(key, "secret", None)
    assert secret in (None, "secret-A"), (
        "相同 key_id 换密被静默覆盖，历史签名不可验证")


def k2_same_key_id_same_secret_idempotent():
    kr = _keyring()
    first = kr.add("k1", "secret-A")
    again = kr.add("k1", "secret-A")
    assert (first is again) or (getattr(first, "secret", None) ==
                                getattr(again, "secret", None) ==
                                "secret-A"), "同密重试应幂等"


probe("M1 deleted-id-rewrite-rejected", m1_deleted_id_cannot_be_rewritten)
probe("M2 derived-from-tombstone-rejected", m2_derived_from_tombstone_rejected)
probe("M3 identity-overwrite-rejected", m3_identity_conflict_rejected)
probe("M4 duplicate-write-real-id", m4_duplicate_write_returns_persisted_id)
probe("U1 stale-usage-not-rebilled", u1_stale_provider_usage_not_rebilled)
probe("K1 key-id-not-rekeyed", k1_same_key_id_different_secret_rejected)
probe("K2 key-add-idempotent[guard]", k2_same_key_id_same_secret_idempotent)

print("=" * 56)
for name, status in results:
    print(f"{status:8} {name}")
failed = [n for n, s in results if s.startswith("FAIL")]
print("=" * 56)
print(f"total={len(results)} pass={len(results)-len(failed)} fail={len(failed)}")
sys.exit(1 if failed else 0)
