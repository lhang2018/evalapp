"""ALN-24-D：用户记忆来源/纠错/删除/范围调整测试。

复用 ALN-05 的存储/墓碑切片；本卡只验证控制面语义（来源可见、纠正事实后续生效、
删除保留项明确、范围策略禁止优先、API 角色与拒绝路径）。
"""

import json
import tempfile
import threading
import urllib.error
import urllib.request

import pytest

from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.memory.store import (Credibility, MemoryItem, MemoryKind,
                                   MemoryStore, Scope)
from evoagent.memory.user_control import (MemoryControlError,
                                          MemoryControlService,
                                          MemoryNotFound)


@pytest.fixture
def store(tmp_path):
    memory = MemoryStore(str(tmp_path / "memory.db"))
    yield memory
    memory.close()


@pytest.fixture
def service(store):
    return MemoryControlService(store)


def seed(store, content="用户住在杭州", *, scope=Scope.USER, scope_key="u1",
         source="user:profile", evidence=None, status=Credibility.VERIFIED,
         kind=MemoryKind.PREFERENCE if hasattr(MemoryKind, "PREFERENCE")
         else MemoryKind.RUNTIME_EXPERIENCE):
    item = MemoryItem(content=content, kind=kind, scope=scope,
                      scope_key=scope_key, source=source, status=status,
                      evidence=evidence or {"eval": "passed"})
    return store.write(item, require_verification=False)


def test_list_and_show_expose_source_without_evidence_snapshot(store, service):
    item = seed(store)
    items = service.list_memories()
    assert len(items) == 1
    public = items[0]
    assert public["provenance"]["source"] == "user:profile"
    assert public["provenance"]["scope"] == "user"
    assert public["privacy"]["storage"] == "local-memory-db"
    assert "evidence" not in public
    detail = service.get_memory(item.memory_id)
    assert detail["content"] == "用户住在杭州"
    with pytest.raises(MemoryNotFound):
        service.get_memory("does-not-exist")


def test_corrected_fact_is_used_by_follow_up_retrieval(store, service):
    item = seed(store)
    result = service.correct_memory(item.memory_id, "用户住在上海",
                                    actor="api:user", reason="用户纠正住址")
    assert result["ok"] is True
    assert result["item"]["content"] == "用户住在上海"
    assert result["item"]["provenance"]["source"] == "user"
    assert result["item"]["status"] == "verified"
    assert result["verified_in_retrieval"] is True
    # GLOBAL-02：后续检索使用纠正事实；旧事实不再命中
    after = store.search("上海", scope=Scope.USER, limit=10)
    assert any(hit.memory_id == item.memory_id and hit.content == "用户住在上海"
               for hit in after)
    old = store.search("杭州", scope=Scope.USER, limit=10)
    assert all(hit.memory_id != item.memory_id for hit in old)


def test_delete_reports_retained_and_failures_without_full_erasure_claim(store, service):
    item = seed(store)
    result = service.delete_memory(item.memory_id, actor="api:user",
                                   reason="用户要求删除")
    assert result["ok"] is True
    assert item.memory_id in result["deleted_ids"]
    assert result["tombstones"]
    assert result["recall_check"]["get_after_delete"] is None
    assert result["recall_check"]["retrieval_excludes_deleted"] is True
    assert result["fully_erased"] is False
    assert {layer["layer"] for layer in result["retained"]} >= {
        "tombstones", "审计日志", "历史备份"}
    assert "不宣称" in result["scope_claim"]
    assert result["failures"] == []
    with pytest.raises(MemoryNotFound):
        service.delete_memory(item.memory_id, actor="api:user", reason="again")


def test_deleted_memory_disappears_from_list_and_search(store, service):
    item = seed(store, content="偏好清晨提醒", scope=Scope.PROJECT,
                scope_key="proj-1")
    service.delete_memory(item.memory_id, actor="api:user", reason="误记")
    assert service.list_memories(scope="project") == []
    assert all(hit.memory_id != item.memory_id
               for hit in store.search("清晨提醒", scope=Scope.PROJECT))


def test_scope_policy_deny_wins_over_allow_and_validates_input(service):
    default = service.get_scope_policy()
    assert default["deny"] == [] and default["all_layers"] is True
    updated = service.set_scope_policy(allow=["user", "project"],
                                       deny=["shared"], actor="api:admin",
                                       reason="只让用户与项目记忆参与分析")
    policy = updated["policy"]
    assert policy["allow"] == ["project", "user"]
    assert policy["deny"] == ["shared"]
    assert service.scope_allowed("user") is True
    assert service.scope_allowed("shared") is False
    assert service.scope_allowed("task") is False
    with pytest.raises(MemoryControlError):
        service.set_scope_policy(allow=["unknown"], deny=[], actor="a",
                                 reason="r")
    with pytest.raises(MemoryControlError):
        service.set_scope_policy(allow=["user"], deny=["user"], actor="a",
                                 reason="r")
    with pytest.raises(MemoryControlError):
        service.set_scope_policy(allow=[], deny=[], actor="a", reason="r")
    with pytest.raises(MemoryControlError):
        service.set_scope_policy(allow=["user"], deny=[], actor="", reason="r")


def test_control_service_rejects_empty_names(store, service):
    item = seed(store)
    with pytest.raises(MemoryControlError):
        service.correct_memory(item.memory_id, " ", actor="a", reason="r")
    with pytest.raises(MemoryControlError):
        service.correct_memory(item.memory_id, "合法内容", actor="a", reason="")
    with pytest.raises(MemoryControlError):
        service.delete_memory(item.memory_id, actor="a", reason="")


# ---------------- HTTP 接口 ----------------

@pytest.fixture
def api(tmp_path):
    app = App(data_dir=str(tmp_path))
    app.config.api_token = "op"
    app.config.api_admin_token = "root"
    server = ApiServer(app, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.port}"
    try:
        yield app, base
    finally:
        server.shutdown()
        app.db.close()


def call(base, method, path, payload=None, token="root"):
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(base + path, data=data,
                                     headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_api_lists_and_shows_memory_with_source(api):
    app, base = api
    item = seed(app.memory, content="用户喜欢简洁回答", scope=Scope.USER,
                scope_key="u1")
    # 既有策略：/memory 前缀属于“全局记忆管理”，仅 admin 可访问；
    # operator 视角需 24-C 会话身份接线后另行放开（保留缺口，见交付文档）。
    status, payload = call(base, "GET", "/memory/items", token="op")
    assert status == 403
    status, payload = call(base, "GET", "/memory/items", token="root")
    assert status == 200
    assert payload["count"] >= 1
    assert any(row["memory_id"] == item.memory_id and
               row["provenance"]["source"] == "user:profile"
               for row in payload["items"])
    status, payload = call(base, "GET", f"/memory/items/{item.memory_id}",
                           token="root")
    assert status == 200 and payload["item"]["content"] == "用户喜欢简洁回答"
    status, _ = call(base, "GET", "/memory/items/nope", token="root")
    assert status == 404


def test_api_correct_and_delete_with_role_checks(api):
    app, base = api
    item = seed(app.memory, content="偏好中文回复")
    status, payload = call(base, "POST",
                           f"/memory/items/{item.memory_id}/correct",
                           {"content": "偏好英文回复", "reason": "用户纠正"},
                           token="root")
    assert status == 200 and payload["verified_in_retrieval"] is True
    status, payload = call(base, "POST",
                           f"/memory/items/{item.memory_id}/delete",
                           {"reason": "用户删除"}, token="root")
    assert status == 200 and payload["fully_erased"] is False
    assert payload["retained"]
    # 无令牌 → 401；未知记忆 → 404（admin 放行后由业务层拒绝）
    status, _ = call(base, "GET", "/memory/items", token="")
    assert status == 401
    status, _ = call(base, "POST", "/memory/items/nope/delete",
                     {"reason": "x"}, token="root")
    assert status == 404


def test_api_scope_policy_requires_admin(api):
    app, base = api
    status, _ = call(base, "PUT", "/memory/scope-policy",
                     {"allow": ["user"], "deny": ["shared"],
                      "reason": "operator 不应被允许"}, token="op")
    assert status == 403
    status, payload = call(base, "PUT", "/memory/scope-policy",
                           {"allow": ["user", "project"], "deny": ["shared"],
                            "reason": "限制分析范围"}, token="root")
    assert status == 200
    assert payload["policy"]["deny"] == ["shared"]
    status, payload = call(base, "GET", "/memory/scope-policy", token="root")
    assert status == 200 and payload["allow"] == ["project", "user"]
