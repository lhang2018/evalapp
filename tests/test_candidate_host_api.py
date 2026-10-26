"""ALN-09-I：候选宿主消费与问题队列管理入口测试。

覆盖：只消费通过摘要/版本校验的真实候选制品；评测结果绑定
baseline/candidate/数据集/宿主参数；成功不自动发布；篡改/未注册/超时
拒绝或如实归因；队列只读视图与过期租约恢复；API 角色门禁。
"""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from evoagent.api.server import ApiServer
from evoagent.app import App
from evoagent.integration.candidate_execution import CandidateEvaluationService


def register(repository, *, asset_id, version, files, entry="main.py"):
    return repository.register(
        asset_id=asset_id, asset_type="capability_pack",
        namespace="shared_validated", version=version,
        content={"entry": entry, "files": dict(files)},
        lifecycle_state="candidate")


def candidate_script(ok=True):
    payload = {"ok": ok, "value": {"ok": ok}}
    return "import json\nprint(json.dumps(" + repr(payload) + "))\n"


def baseline_script(ok=False):
    payload = {"ok": ok, "value": {"ok": ok}}
    return "import json\nprint(json.dumps(" + repr(payload) + "))\n"


CASES = [{"case_id": "c1", "input": {"q": 1}}, {"case_id": "c2", "input": {"q": 2}}]


@pytest.fixture
def service(tmp_path):
    import sqlite3
    from evoagent.assets.repository import AssetRepository
    connection = sqlite3.connect(str(tmp_path / "assets.db"),
                                 check_same_thread=False)
    repository = AssetRepository(connection)
    yield CandidateEvaluationService(repository)
    connection.close()


def test_evaluate_improved_candidate_bound_to_identities(service):
    baseline = register(service.repository, asset_id="base.pack", version="1.0.0",
                        files={"main.py": baseline_script()})
    candidate = register(service.repository, asset_id="cand.pack", version="2.0.0",
                         files={"main.py": candidate_script(True)})
    result = service.evaluate(
        candidate_id="cand_test", baseline_ref=baseline.ref,
        candidate_ref=candidate.ref, cases=CASES, dataset_ref="builtin_smoke",
        actor="admin", reason="受控评测")
    assert result["ok"] is True
    assert result["verdict"] == "improved", result["report"]["benefit"]
    assert result["release_decision"]["allow"] is True
    assert result["baseline"]["asset_ref"] == f"{baseline.asset_id}@{baseline.version}"
    assert result["candidate"]["asset_ref"] == f"{candidate.asset_id}@{candidate.version}"
    assert result["dataset_ref"] == "builtin_smoke"
    assert result["host"]["benchmark_version"]
    assert result["evidence_digest"]
    assert result["auto_published"] is False
    assert result["release_before"] == result["release_after"]


def test_tampered_or_missing_asset_is_rejected(service):
    baseline = register(service.repository, asset_id="base.pack", version="1.0.0",
                        files={"main.py": baseline_script()})
    with pytest.raises(Exception):
        service.evaluate(candidate_id="c", baseline_ref=baseline.ref,
                         candidate_ref="nope@9.9.9", cases=CASES,
                         actor="admin", reason="x")
    # 非代码资产（缺少 content.files）也拒绝
    service.repository.register(
        asset_id="bad.pack", asset_type="capability_pack",
        namespace="shared_validated", version="1.0.0",
        content={"text": "not code"}, lifecycle_state="candidate")
    with pytest.raises(Exception):
        service.evaluate(candidate_id="c", baseline_ref=baseline.ref,
                         candidate_ref="bad.pack@1.0.0", cases=CASES,
                         actor="admin", reason="x")
    with pytest.raises(Exception):
        service.evaluate(candidate_id="c", baseline_ref=baseline.ref,
                         candidate_ref=baseline.ref, cases=[],
                         actor="admin", reason="x")


def test_regressed_candidate_blocks_release(service):
    baseline = register(service.repository, asset_id="base.pack", version="1.0.0",
                        files={"main.py": candidate_script(True)})
    candidate = register(service.repository, asset_id="cand.pack", version="2.0.0",
                         files={"main.py": candidate_script(False)})
    result = service.evaluate(
        candidate_id="cand_test", baseline_ref=baseline.ref,
        candidate_ref=candidate.ref, cases=CASES, actor="admin", reason="x")
    assert result["verdict"] in ("regressed", "inconclusive")
    assert result["release_decision"]["allow"] is False
    assert result["release_decision"]["blockers"]
    assert result["release_before"] == result["release_after"]


def test_timeout_candidate_is_attributed_without_publish(service):
    baseline = register(service.repository, asset_id="base.pack", version="1.0.0",
                        files={"main.py": baseline_script()})
    slow = register(service.repository, asset_id="slow.pack", version="1.0.0",
                    files={"main.py": "import time\ntime.sleep(10)\n"})
    service.benchmark.timeout = 1.0
    result = service.evaluate(
        candidate_id="cand_slow", baseline_ref=baseline.ref,
        candidate_ref=slow.ref, cases=[{"case_id": "c1", "input": {}}],
        actor="admin", reason="超时归因")
    assert result["auto_published"] is False
    assert result["release_decision"]["allow"] is False


@pytest.fixture
def api(tmp_path):
    app = App(data_dir=str(tmp_path))
    app.config.api_token = "op"
    app.config.api_admin_token = "root"
    register(app.candidate_execution.repository, asset_id="base.pack",
             version="1.0.0", files={"main.py": baseline_script()})
    register(app.candidate_execution.repository, asset_id="cand.pack",
             version="2.0.0", files={"main.py": candidate_script(True)})
    server = ApiServer(app, host="127.0.0.1", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield app, f"http://127.0.0.1:{server.port}"
    server.shutdown()
    app.db.close()


def call(base, method, path, payload=None, token="root"):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(base + path, data=data, headers=headers,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_api_host_evaluation_and_role_gate(api):
    app, base = api
    candidate = app.evolution.propose_candidate(
        "v0", "expert/travel", "路由错误率", "提高并发", change_scope="expert")
    body = {"candidate_id": candidate.candidate_id,
            "baseline_ref": "base.pack@1.0.0",
            "candidate_ref": "cand.pack@2.0.0",
            "cases": CASES, "dataset_ref": "builtin_smoke",
            "reason": "API 受控评测"}
    status, payload = call(base, "POST",
                           "/evolution/candidate-host/evaluate", body)
    assert status == 200 and payload["ok"] is True
    assert payload["auto_published"] is False
    assert payload["candidate"]["asset_ref"] == "cand.pack@2.0.0"
    assert app.releases.current_release_id == payload["release_before"]
    status, _ = call(base, "POST", "/evolution/candidate-host/evaluate",
                     body, token="op")
    assert status == 403
    status, _ = call(base, "POST", "/evolution/candidate-host/evaluate",
                     {**body, "candidate_id": "nope"})
    assert status == 404
    status, _ = call(base, "POST", "/evolution/candidate-host/evaluate",
                     {**body, "candidate_ref": "missing@1.0.0"})
    assert status == 409


def test_api_queue_view_and_recover_expired_lease(api):
    from evoagent.evolution.controller import ProblemTicket
    app, base = api
    ticket = ProblemTicket(ticket_id="tk_09i", description="队列管理探针",
                           signal_type="failure_rate",
                           target_component="expert/travel",
                           hypothesis="x", change_scope="expert")
    assert app.evolution.problem_queue.enqueue(ticket) is True
    status, payload = call(base, "GET", "/evolution/candidate-host/queue")
    assert status == 200
    assert payload["stats"]["pending"] == 1
    assert payload["pending"][0]["ticket_id"] == "tk_09i"
    claim = app.evolution.problem_queue.claim("tk_09i")
    assert claim is not None
    app.db._conn.execute(
        "UPDATE evolution_problem_queue SET lease_expires_at=0"
        " WHERE ticket_id='tk_09i'")
    app.db._conn.commit()
    status, payload = call(base, "POST",
                           "/evolution/candidate-host/queue/recover",
                           {"reason": "过期租约恢复"})
    assert status == 200 and payload["recovered"] == 1
    status, payload = call(base, "GET", "/evolution/candidate-host/queue")
    assert payload["stats"]["pending"] == 1
    status, _ = call(base, "GET", "/evolution/candidate-host/queue",
                     token="op")
    assert status == 403
