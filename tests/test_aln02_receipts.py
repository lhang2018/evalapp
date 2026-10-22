"""ALN-02: real receipts, atomic recovery and verified external compensation."""
import json
import os
import subprocess
import sys
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest

from evoagent.core.persistence import Database
from evoagent.core.permissions import PermissionController
from evoagent.tools.contract import ToolContract, ToolResult
from evoagent.tools.registry import ToolRegistry
from evoagent.workflows.engine import WorkflowEngine
from evoagent.workflows.models import WorkflowDef, WorkflowNode, WorkflowNodeState


def ok(data=None):
    return ToolResult(True, data=data, submitted=True, executed=True, contract_valid=True,
                      goal_reached=True, independently_verified=True)


@pytest.fixture
def gateway(tmp_path):
    db = Database(str(tmp_path / "effects.db"))
    tools = ToolRegistry(PermissionController(high_risk_mode="allow"), db)
    yield tools
    db.close()


def unknown(tools, *, reconciler=None):
    tools.register(ToolContract("send", side_effect_level="external", reconcile=reconciler),
        lambda a: ToolResult(False, side_effect_uncertain=True, error="reply lost"))
    result = tools.execute("send", {"target": "recipient"}, task_id="task", idempotency_key="operation")
    return result.evidence["effect_id"]


def confirmed(operation, receipt):
    result = ok({"restored_artifact": "receipt-output"})
    result.evidence["external_receipt_ref"] = "provider:receipt-1"
    return result


def test_verified_receipt_restores_result_across_database_restart(gateway):
    identity = unknown(gateway, reconciler=confirmed)
    gateway.reconcile_effect(identity, {"ref": "receipt-1"})
    db = Database(gateway.store.path)
    try:
        fresh = ToolRegistry(PermissionController(high_risk_mode="deny"), db)
        calls = []
        fresh.register(ToolContract("send", side_effect_level="external"), lambda a: calls.append(1) or ok())
        result = fresh.execute("send", {"target": "recipient"}, task_id="task", idempotency_key="operation")
        assert result.ok and result.evidence["replayed"]
        assert result.data == {"restored_artifact": "receipt-output"} and calls == []
        assert fresh.effects.get(identity)["last_reconciled_at"]
    finally:
        db.close()


@pytest.mark.parametrize("damage", ["unverified", "uncertain", "no_receipt_ref", "wrong_version", "no_adapter"])
def test_unverifiable_receipt_never_releases_pending_operation(gateway, damage):
    def reconcile(operation, receipt):
        result = confirmed(operation, receipt)
        if damage == "unverified": result.independently_verified = False
        if damage == "uncertain": result.side_effect_uncertain = True
        if damage == "no_receipt_ref": result.evidence.clear()
        return result
    identity = unknown(gateway, reconciler=None if damage == "no_adapter" else reconcile)
    if damage == "wrong_version": gateway.contract("send").tool_version = "2"
    with pytest.raises(ValueError):
        gateway.reconcile_effect(identity, {})
    assert gateway.effects.get(identity)["status"] == "unknown"


def test_reconciliation_is_atomic_when_final_state_write_fails(gateway):
    identity = unknown(gateway, reconciler=confirmed)
    gateway.store._conn.execute("CREATE TRIGGER deny_confirmation BEFORE UPDATE ON task_side_effects "
        "WHEN NEW.status='confirmed' BEGIN SELECT RAISE(ABORT,'disk failed'); END")
    gateway.store._conn.commit()
    with pytest.raises(Exception, match="disk failed"):
        gateway.reconcile_effect(identity, {})
    assert gateway.effects.get(identity)["status"] == "unknown"
    assert gateway.store.get_tool_execution("effect:" + identity) is None


def test_proven_not_executed_receipt_allows_one_fresh_attempt(gateway):
    def not_sent(operation, receipt):
        result = ToolResult(False, executed=False, independently_verified=True)
        result.evidence["external_receipt_ref"] = "provider:not-sent"
        return result
    identity = unknown(gateway, reconciler=not_sent)
    gateway.reconcile_effect(identity, {})
    calls = []
    gateway._tools["send"].runner = lambda a: calls.append(1) or ok()
    assert gateway.execute("send", {"target": "recipient"}, task_id="task", idempotency_key="operation").ok
    assert calls == [1] and len(gateway.effects.history("task")) == 2


def test_live_timed_out_thread_is_not_declared_cancelled_by_another_registry(gateway):
    release, finished = threading.Event(), threading.Event()
    def delayed(args):
        release.wait(5)
        finished.set()
        return ok()
    gateway.register(ToolContract("send", side_effect_level="external", timeout_policy=.02,
                                  reconcile=confirmed), delayed)
    result = gateway.execute("send", {}, task_id="task", idempotency_key="slow")
    identity = result.evidence["effect_id"]
    second = ToolRegistry(PermissionController(high_risk_mode="allow"), gateway.store)
    second.register(gateway.contract("send"), delayed)
    try:
        with pytest.raises(ValueError, match="仍在运行"):
            second.reconcile_effect(identity, {})
    finally:
        release.set()
        assert finished.wait(2)
    assert gateway.effects.get(identity)["status"] == "unknown"


def test_timeout_while_final_guard_waits_still_blocks_replay(gateway, monkeypatch):
    release, finished = threading.Event(), threading.Event()
    checks = []
    original = gateway.authorizations.check
    def held_guard(*args, **kwargs):
        checks.append(1)
        if len(checks) == 3:
            release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(gateway.authorizations, 'check', held_guard)
    calls = []
    gateway.register(ToolContract('send', side_effect_level='external', timeout_policy=.02),
        lambda a: calls.append(1) or finished.set() or ok())
    try:
        result = gateway.execute('send', {}, task_id='task', idempotency_key='guard-blocked')
        assert result.side_effect_uncertain and result.required_action == 'reconcile'
        assert not result.safe_to_repeat and calls == []
        assert gateway.execute('send', {}, task_id='task', idempotency_key='guard-blocked').side_effect_uncertain
    finally:
        release.set()
        assert finished.wait(2)
    assert calls == [1]


def test_real_http_provider_receipt_is_fetched_and_bound_to_original_key(gateway):
    records = {}
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            records[body['key']] = {**body, 'artifact': 'real-provider-output', 'receipt': 'r1'}
            self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
        def do_GET(self):
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps(records[self.path[1:]]).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    def send(args):
        request = urllib.request.Request(base, data=json.dumps({'key': 'http-operation', 'target': args['target']}).encode())
        with urllib.request.urlopen(request): pass
        return ToolResult(False, side_effect_uncertain=True, error='response lost')
    def reconcile(operation, supplied_receipt):
        with urllib.request.urlopen(base + '/' + operation['idempotency_key']) as response:
            actual = json.load(response)
        assert actual['target'] == operation['args']['target']
        result = ok({'artifact': actual['artifact']})
        result.evidence['external_receipt_ref'] = actual['receipt']
        return result
    try:
        gateway.register(ToolContract('send', side_effect_level='external', reconcile=reconcile), send)
        result = gateway.execute('send', {'target': 'recipient'}, task_id='task', idempotency_key='http-operation')
        recovered = gateway.reconcile_effect(result.evidence['effect_id'], {'untrusted': 'cannot replace GET'})
        assert recovered.data == {'artifact': 'real-provider-output'} and len(records) == 1
    finally:
        server.shutdown(); thread.join(2); server.server_close()


def file_workflow(tools, path, *, restore_badly=False, restore_unknown=False):
    def write(args):
        previous = Path(args['path']).read_text(encoding='utf-8')
        Path(args['path']).write_text('changed', encoding='utf-8')
        return ok({'previous': previous})
    def restore(args):
        if restore_unknown:
            return ToolResult(False, side_effect_uncertain=True)
        Path(args['path']).write_text('wrong' if restore_badly else args['content'], encoding='utf-8')
        return ok()
    def verify(args):
        actual = Path(args['path']).read_text(encoding='utf-8')
        result = ok(actual); result.goal_reached = actual == args['expected']; return result
    tools.register(ToolContract('write', side_effect_level='local'), write)
    tools.register(ToolContract('restore', side_effect_level='local'), restore)
    tools.register(ToolContract('verify'), verify)
    tools.register(ToolContract('fail'), lambda a: ToolResult(False, error='known failure', error_class='invalid_arguments'))
    return WorkflowDef('restore-file', created_from_task='task', nodes=[
        WorkflowNode('write', tool='write', args={'path': str(path)}),
        WorkflowNode('fail', tool='fail', deps=['write'])], compensation_actions={
        'write': {'tool': 'restore', 'args': {'path': '$args.path', 'content': '$output.previous'},
                  'verify_tool': 'verify', 'verify_args': {'path': '$args.path', 'expected': '$output.previous'}}})


@pytest.mark.parametrize('bad,unknown_result', [(False, False), (True, False), (False, True)])
def test_compensation_uses_original_arguments_and_verifies_real_file(gateway, tmp_path, bad, unknown_result):
    path = tmp_path/'original.txt'; path.write_text('original', encoding='utf-8')
    wf = file_workflow(gateway, path, restore_badly=bad, restore_unknown=unknown_result)
    result = WorkflowEngine(gateway).run(wf, {})
    original = next(row for row in gateway.effects.history('task') if row['tool_id'] == 'write')
    if unknown_result:
        assert result.status == WorkflowNodeState.WAITING_EXTERNAL
    elif bad:
        assert not result.compensations[0]['verified'] and original['status'] == 'confirmed'
    else:
        assert path.read_text(encoding='utf-8') == 'original'
        assert result.compensations[0]['verified'] and original['status'] == 'compensated'
    assert result.tool_results['write']['executed'] is True
    assert not result.ok  # Restoration does not turn the failed task into success.


def test_compensation_cannot_bypass_task_authorization(gateway, tmp_path):
    path = tmp_path/'original.txt'; path.write_text('original', encoding='utf-8')
    wf = file_workflow(gateway, path)
    gateway.authorizations.issue('task', {'tools': ['write', 'fail', 'verify'], 'permissions': []}, actor='admin')
    result = WorkflowEngine(gateway).run(wf, {})
    assert not result.compensations[0]['verified']
    assert path.read_text(encoding='utf-8') == 'changed'
    assert not any(row['tool_id'] == 'restore' for row in gateway.effects.history('task'))


def test_successful_external_tool_is_not_reexecuted_after_bad_node_output(gateway):
    calls = []
    gateway.register(ToolContract('send', side_effect_level='external'), lambda a: calls.append(1) or ok({'wrong': True}))
    wf = WorkflowDef('output-shape', created_from_task='task', nodes=[WorkflowNode('n', tool='send',
        output_schema={'type': 'object', 'required': ['expected']}, max_retries=5)])
    result = WorkflowEngine(gateway).run(wf, {})
    assert not result.ok and calls == [1]
    assert result.tool_results['n']['executed'] and not result.tool_results['n']['contract_valid']


@pytest.mark.parametrize('phase', ['planned', 'before', 'after'])
def test_process_crash_preserves_intent_and_requires_real_state_reconciliation(tmp_path, phase):
    database = tmp_path/'crash.db'; artifact = tmp_path/'artifact.txt'
    source = '''
import os, sys
from pathlib import Path
from evoagent.core.persistence import Database
from evoagent.tools.registry import ToolRegistry
from evoagent.tools.contract import ToolContract
db=Database(sys.argv[1]); tools=ToolRegistry(store=db)
if sys.argv[3]=='planned': tools.effects.transition=lambda *a,**kw: os._exit(17)
def runner(args):
    if sys.argv[3]=='after': Path(args['path']).write_text('delivered')
    os._exit(17)
tools.register(ToolContract('send',side_effect_level='external'),runner)
tools.execute('send',{'path':sys.argv[2]},task_id='task',idempotency_key='operation')
'''
    result = subprocess.run([sys.executable, '-c', source, str(database), str(artifact), phase],
        capture_output=True, text=True, timeout=15, env={**os.environ, 'PYTHONUTF8': '1'})
    assert result.returncode == 17, result.stderr
    db = Database(str(database))
    try:
        tools = ToolRegistry(store=db)
        [row] = tools.effects.pending('task')
        assert row['status'] == ('planned' if phase == 'planned' else 'submitted')
        assert tools.effects.operation(row['effect_id'])['args']['path'] == str(artifact)
        calls = []
        def reconcile(operation, receipt):
            result = ok(artifact.read_text()) if artifact.exists() else ToolResult(False, independently_verified=True)
            result.evidence['external_receipt_ref'] = 'filesystem:observed'
            return result
        tools.register(ToolContract('send',side_effect_level='external',reconcile=reconcile), lambda a: calls.append(1) or ok())
        assert tools.execute('send', {'path': str(artifact)}, task_id='task', idempotency_key='operation').side_effect_uncertain
        assert not calls
        recovered = tools.reconcile_effect(row['effect_id'], {})
        assert recovered.ok is (phase == 'after')
        assert not tools.effects.pending('task')
    finally:
        db.close()


def test_real_app_compensation_waits_for_approval_then_restores_file(tmp_path):
    from evoagent.app import App
    from evoagent.core.task import Task, TaskStatus
    app = App(data_dir=str(tmp_path/'app'))
    path = tmp_path/'original.txt'; path.write_text('original', encoding='utf-8')
    try:
        app.permissions.high_risk_mode = 'allow'
        wf = file_workflow(app.tools, path)
        def fail(args):
            app.permissions.high_risk_mode = 'confirm'
            return ToolResult(False,error='known failure',error_class='invalid_arguments')
        app.tools._tools['fail'].runner = fail
        app._workflows[wf.workflow_id] = wf
        task = app.controller.execute(Task(user_goal='test restoration', workflow_ref=wf.workflow_id), intent_hint='general')
        assert task.task_status == TaskStatus.SUSPENDED
        assert task.waiting_approvals and path.read_text(encoding='utf-8') == 'changed'
        resumed, accepted = app.controller.decide_approval(task.waiting_approvals[0], True, actor='test-admin')
        assert accepted and resumed.task_status == TaskStatus.FAILED
        assert path.read_text(encoding='utf-8') == 'original'
        assert any(e['tool_id']=='write' and e['status']=='compensated' for e in app.tools.effects.history(task.task_id))
    finally:
        app.db.close()


def test_intent_and_arguments_are_claimed_in_one_transaction(gateway):
    calls = []
    gateway.register(ToolContract('send', side_effect_level='external'), lambda a: calls.append(1) or ok())
    gateway.store._conn.execute("CREATE TRIGGER deny_operation BEFORE INSERT ON effect_receipts "
        "BEGIN SELECT RAISE(ABORT,'operation disk failed'); END")
    gateway.store._conn.commit()
    result = gateway.execute('send', {'target': 'recipient'}, task_id='task')
    assert not result.ok and not calls
    assert gateway.effects.history('task') == []


def test_confirmed_result_cannot_be_reinterpreted_as_a_new_tool_version(gateway):
    calls = []
    gateway.register(ToolContract('send', side_effect_level='external'), lambda a: calls.append(1) or ok())
    assert gateway.execute('send', {}, task_id='task', idempotency_key='operation').ok
    gateway.contract('send').tool_version = '2'
    result = gateway.execute('send', {}, task_id='task', idempotency_key='operation')
    assert not result.ok and calls == [1]


def test_compensation_reverses_dependencies_even_when_definition_is_unsorted(gateway, tmp_path):
    path = tmp_path/'order.txt'; path.write_text('A', encoding='utf-8')
    wf = file_workflow(gateway, path)
    def write(args):
        previous = path.read_text(encoding='utf-8')
        path.write_text(args['content'], encoding='utf-8')
        return ok({'previous': previous})
    gateway._tools['write'].runner = write
    first = WorkflowNode('first', tool='write', args={'path': str(path), 'content': 'B'})
    second = WorkflowNode('second', tool='write', deps=['first'], args={'path': str(path), 'content': 'C'})
    failed = WorkflowNode('fail', tool='fail', deps=['second'])
    wf.nodes = [second, first, failed]
    policy = wf.compensation_actions['write']
    wf.compensation_actions = {'first': policy, 'second': policy}
    result = WorkflowEngine(gateway).run(wf, {})
    assert [row['node'] for row in result.compensations] == ['second', 'first']
    assert all(row['verified'] for row in result.compensations)
    assert path.read_text(encoding='utf-8') == 'A'


def test_compensation_verification_budget_exhaustion_pauses_instead_of_claiming_rollback(gateway, tmp_path):
    from evoagent.core.budget import BudgetController, BudgetLimits
    path = tmp_path/'budget.txt'; path.write_text('original', encoding='utf-8')
    wf = file_workflow(gateway, path)
    # write + failed node + restoration consume the three permitted calls.
    result = WorkflowEngine(gateway).run(wf, {}, budget=BudgetController(BudgetLimits(max_tool_calls=3)))
    assert result.status == WorkflowNodeState.PAUSED
    assert not result.compensations[0]['verified']
    assert next(e for e in gateway.effects.history('task') if e['tool_id']=='write')['status'] == 'confirmed'


def test_same_task_mutating_nodes_do_not_race_pending_ledger(gateway):
    entered, release, second = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def runner(args):
        calls.append(args['n'])
        if args['n'] == 1:
            entered.set(); assert release.wait(3)
        else:
            second.set()
        return ok()
    gateway.register(ToolContract('send', side_effect_level='external'), runner)
    wf = WorkflowDef('serial-effects', created_from_task='task', nodes=[
        WorkflowNode('first', tool='send', args={'n': 1}), WorkflowNode('second', tool='send', args={'n': 2})])
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(WorkflowEngine(gateway, max_parallel=2).run, wf, {})
        try:
            assert entered.wait(2)
            assert not second.wait(.05)
        finally:
            release.set()
        result = future.result(timeout=3)
    assert result.ok and calls == [1, 2]


def test_unknown_effect_stops_remaining_nodes_in_mutating_batch(gateway):
    calls = []
    gateway.register(ToolContract('send', side_effect_level='external'),
        lambda args: calls.append(args['n']) or ToolResult(False, side_effect_uncertain=True))
    wf = WorkflowDef('unknown-stops-batch', created_from_task='task', nodes=[
        WorkflowNode('first', tool='send', args={'n': 1}), WorkflowNode('second', tool='send', args={'n': 2})])
    result = WorkflowEngine(gateway, max_parallel=2).run(wf, {})
    assert result.status == WorkflowNodeState.WAITING_EXTERNAL
    assert calls == [1] and wf.node('second').state == WorkflowNodeState.PENDING
