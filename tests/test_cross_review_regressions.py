"""Cross-review regressions for the asset and model work streams."""
import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from evoagent.api.auth import ApiAuth
from evoagent.assets.entity import AssetValidationError, DependencyError
from evoagent.assets.repository import AssetRepository
from evoagent.assets.refs import manifest_asset_ref, verify_manifest_ref
from evoagent.models.base import ChatMessage, ModelProfile, ModelRegistry
from evoagent.models.metering import MeteredModel, estimate_cost_optional, pricing_status
from evoagent.models.openai_compat import OpenAICompatAdapter, _usage_of
from evoagent.models.router import ModelRouter


@pytest.fixture
def repo():
    conn = sqlite3.connect(":memory:")
    yield AssetRepository(conn)
    conn.close()


def register(repo, identity, **kwargs):
    return repo.register(asset_id=identity, version="1", asset_type="skill",
        namespace="domain_private", content={"name": identity},
        source_refs=["test:cross-review"], **kwargs)


def test_production_server_registers_asset_api_without_test_side_imports(tmp_path):
    code = """
import json, threading, urllib.request
from evoagent.app import App
from evoagent.api.server import ApiServer
app = App(data_dir=__import__('sys').argv[1])
app.config.api_token = 'operator'
app.config.api_admin_token = 'admin'
server = ApiServer(app, host='127.0.0.1', port=0)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    base = f'http://127.0.0.1:{server.port}/assets'
    request = urllib.request.Request(base, headers={'Authorization':'Bearer operator'})
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
        assert json.loads(response.read())['assets'] == []
    payload = dict(asset_id='demo',asset_type='skill',namespace='domain_private',version='1',content={})
    request = urllib.request.Request(base,data=json.dumps(payload).encode(),headers={'Authorization':'Bearer operator'})
    try:
        urllib.request.urlopen(request, timeout=5)
        raise AssertionError('operator wrote asset')
    except __import__('urllib.error').error.HTTPError as error:
        assert error.code == 403
    request.add_header('Authorization','Bearer admin')
    with urllib.request.urlopen(request,timeout=5) as response:
        assert response.status == 201
finally:
    server.shutdown()
    thread.join(timeout=5)
    app.db.close()
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)],
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("path", ["/assets", "/assets/demo/1/lifecycle"])
def test_asset_writes_require_admin_but_reads_allow_operator(path):
    auth = ApiAuth(token="reader", admin_token="admin")
    assert auth.check("Bearer reader", "POST", path).status == 403
    assert auth.check("Bearer admin", "POST", path).ok
    assert auth.check("Bearer reader", "GET", "/assets").ok


@pytest.mark.parametrize("consumer", ["get", "manifest", "lifecycle"])
def test_asset_body_corruption_cannot_pass_a_stored_digest(repo, consumer):
    asset = register(repo, "demo")
    frozen = manifest_asset_ref(asset)
    repo._conn.execute("UPDATE assets SET content_json=?", ('{"name":"changed"}',))
    repo._conn.commit()
    with pytest.raises(AssetValidationError):
        if consumer == "get":
            repo.get_exact("demo@1")
        elif consumer == "manifest":
            verify_manifest_ref(frozen, repo)
        else:
            repo.transition("demo", "1", "candidate")


def test_gc_keeps_exact_rollback_target_until_referencing_asset_is_collected(repo):
    register(repo, "old")
    register(repo, "new", rollback_ref="old@1")
    repo.transition("old", "1", "retired")
    assert "old@1" not in {f"{a['asset_id']}@{a['version']}" for a in repo.gc_candidates()}
    with pytest.raises(DependencyError):
        repo.collect("old", "1")
    repo.transition("new", "1", "retired")
    repo.collect("new", "1")
    assert repo.collect("old", "1")


def test_transitive_dependency_pins_are_checked_before_root_promotion(repo):
    register(repo, "leaf")
    register(repo, "middle", dependency_refs=["leaf@1"],
             compatibility={"pins": {"leaf": "0" * 64}})
    root = register(repo, "root", dependency_refs=["middle@1"])
    with pytest.raises(DependencyError):
        repo.check_dependencies(root)


@pytest.mark.parametrize("value", [-1, 1.5, True, "bad", float("nan"), float("inf")])
def test_invalid_provider_tokens_are_not_accepted_as_real_usage(value):
    assert _usage_of({"prompt_tokens": value, "completion_tokens": 2}) is None


def test_provider_total_cannot_be_less_than_input_and_output():
    assert _usage_of({"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 1}) is None


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), "bad"])
def test_invalid_prices_remain_unknown(value):
    profile = ModelProfile("bad", pricing_known=True, price_per_1k_input=value)
    assert pricing_status(profile) == "unknown"


class Adapter:
    def __init__(self, profile):
        self._profile = profile
        self.last_usage = None

    def profile(self): return self._profile

    def generate(self, messages):
        count = int(messages[0].content)
        self.last_usage = {"input_tokens": count, "output_tokens": 1}
        self.last_request_id = f"request-{count}"
        return "answer"


def test_concurrent_tasks_settle_their_own_model_usage():
    model = MeteredModel(Adapter(ModelProfile("paid", pricing_known=True, price_per_1k_input=1)))
    returned = threading.Barrier(2)
    def call(count):
        model.generate([ChatMessage("user", str(count))])
        returned.wait(timeout=5)
        return model.last_usage.input_tokens, model.last_usage.request_id
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(call, count) for count in (10, 20)]
        assert [f.result(timeout=8) for f in futures] == [(10, "request-10"), (20, "request-20")]


def test_malformed_usage_does_not_replace_success_with_metering_error():
    adapter = Adapter(ModelProfile("paid", pricing_known=True, price_per_1k_input=1))
    adapter.generate = lambda messages: "answer"
    adapter.last_usage = {"input_tokens": "invalid", "output_tokens": 1}
    model = MeteredModel(adapter)
    assert model.generate([ChatMessage("user", "input")]) == "answer"
    assert model.last_usage.token_source == "estimate"


@pytest.mark.parametrize("path", ["explicit", "default", "fallback"])
def test_unavailable_models_are_never_resolved_or_selected(path):
    registry = ModelRegistry()
    offline = ModelProfile("offline", available=False)
    online = ModelProfile("online")
    for profile in (offline, online): registry.register(profile)
    router = ModelRouter(registry, Adapter(offline if path == "fallback" else online))
    router.setup(lambda identity: Adapter(registry.get(identity)))
    if path == "explicit": router.bind_route("coding", "offline")
    if path == "default": router.set_default_model("offline")
    assert router.resolve("offline") is None
    chosen, _ = router.decide("coding", "general")
    assert chosen.profile().model_id == "online"


def test_missing_vision_model_does_not_fall_back_to_incompatible_adapter():
    router = ModelRouter(ModelRegistry(), Adapter(ModelProfile("text")))
    with pytest.raises(ValueError):
        router.decide("coding", "general", require_vision=True)


@pytest.mark.parametrize("failure", ["http", "transport"])
def test_provider_failures_do_not_echo_secrets(monkeypatch, failure):
    import requests
    secret = "provider-secret-should-not-leak"
    def post(*args, **kwargs):
        if failure == "transport": raise requests.Timeout(secret)
        return SimpleNamespace(status_code=400, text=secret)
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr("evoagent.models.openai_compat.time.sleep", lambda _: None)
    adapter = OpenAICompatAdapter("model", secret)
    with pytest.raises(RuntimeError) as error:
        adapter.generate([ChatMessage("user", "input")])
    assert secret not in str(error.value)


def test_estimated_cost_overflow_remains_unknown():
    p = ModelProfile("overflow", pricing_known=True, price_per_1k_input=1e308)
    assert estimate_cost_optional(p, 100, 100) is None


def test_unknown_price_gets_no_free_cost_advantage_in_routing():
    registry = ModelRegistry()
    unknown = ModelProfile("unknown", quality_score=.8, avg_latency_s=1)
    known = ModelProfile("known", pricing_known=True, price_per_1k_input=1, quality_score=.8, avg_latency_s=1)
    for p in (unknown, known): registry.register(p)
    router = ModelRouter(registry, Adapter(unknown))
    router.setup(lambda identity: Adapter(registry.get(identity)))
    assert router.decide("general", "general", prefer_cost=True)[0].profile().model_id == "known"
    assert router.score(unknown) <= router.score(known)
    rows = router.rank(prefer_cost=True)
    assert rows[0]["model_id"] == "known" and rows[1]["price"] is None


def test_unresolvable_top_model_does_not_hide_other_capable_candidates():
    registry = ModelRegistry()
    for p in (ModelProfile("best", quality_score=1, supports_vision=True),
              ModelProfile("next", quality_score=.8, supports_vision=True)):
        registry.register(p)
    router = ModelRouter(registry, Adapter(ModelProfile("text")))
    router.setup(lambda identity: Adapter(registry.get(identity)) if identity == "next" else None)
    assert router.decide("general", "general", require_vision=True)[0].profile().model_id == "next"


@pytest.mark.parametrize("table,identity,column", [
    ("release_manifests", "release_id", "data"),
    ("runtime_manifests", "manifest_id", "data"),
    ("tasks", "task_id", "data"),
    ("task_runs", "run_id", "version_manifest"),
])
def test_gc_keeps_versions_pinned_by_frozen_release_or_runtime(repo, table, identity, column):
    asset = register(repo, "pinned")
    register(repo, "unrelated")
    repo._conn.execute(f"CREATE TABLE {table}({identity} TEXT PRIMARY KEY,{column} TEXT)")
    payload = {"version_manifest": {"asset_refs": [manifest_asset_ref(asset)]}}
    repo._conn.execute(f"INSERT INTO {table} VALUES (?,?)", ("record", json.dumps(payload)))
    repo._conn.commit()
    for name in ("pinned", "unrelated"): repo.transition(name, "1", "retired")
    assert [a["asset_id"] for a in repo.gc_candidates()] == ["unrelated"]
    with pytest.raises(DependencyError): repo.collect("pinned", "1")
    assert repo.collect("unrelated", "1")
