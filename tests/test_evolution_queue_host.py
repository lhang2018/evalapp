"""ALN-09-H：隔离候选宿主、持久问题队列与多进程恢复测试。

覆盖卡片的验收路径：
- 超时强杀：候选资产里的死循环/长时间 sleep 被强杀，返回 timeout；
- 网络/凭据边界：候选资产仍受同一审计钩子约束，生产凭据不继承；
- 崩溃后队列和候选身份可恢复：两个 Database 连接模拟两个进程，
  租约过期可接管、旧持有者被 fencing 拒绝、完成后的 candidate_id 重启仍在。
"""

import os
import sqlite3
import tempfile
import unittest

from evoagent.assets.repository import AssetRepository
from evoagent.core.leases import LeaseLost
from evoagent.core.persistence import Database
from evoagent.evolution.controller import EvolutionController, ProblemTicket
from evoagent.evolution.host import (CandidateAssetError, IsolatedCandidateHost)
from evoagent.evolution.queue import (ProblemQueue, STATUS_CLAIMED,
                                      STATUS_CONSUMED, STATUS_PENDING)

SIGNAL = "failure_rate"
COMPONENT = "expert/travel"


def _ticket(ticket_id="tk_1", **overrides):
    values = dict(ticket_id=ticket_id, description="路由错误率升高",
                  signal_type=SIGNAL, target_component=COMPONENT,
                  hypothesis="增加意图白名单", change_scope="expert")
    values.update(overrides)
    return ProblemTicket(**values)


class TestPersistentProblemQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_queue_")
        self.path = os.path.join(self.tmp, "queue.db")
        self.now = [1000.0]
        self.db = Database(self.path)
        self.queue = ProblemQueue(self.db, ttl=5.0,
                                  clock=lambda: self.now[0], owner_id="worker_a")

    def tearDown(self):
        self.db._conn.close()

    def _second_process(self):
        db = Database(self.path)
        self.addCleanup(db._conn.close)
        return db, ProblemQueue(db, ttl=5.0, clock=lambda: self.now[0],
                                owner_id="worker_b")

    def test_enqueue_is_idempotent_and_persists(self):
        self.assertTrue(self.queue.enqueue(_ticket()))
        self.assertFalse(self.queue.enqueue(_ticket()))
        self.assertEqual(self.queue.stats()["pending"], 1)
        db2, queue2 = self._second_process()
        self.assertEqual(queue2.get("tk_1")["status"], STATUS_PENDING)
        self.assertEqual(queue2.pending()[0]["ticket"].ticket_id, "tk_1")

    def test_claim_is_exclusive_across_connections(self):
        self.queue.enqueue(_ticket())
        first = self.queue.claim("tk_1")
        self.assertIsNotNone(first)
        _, queue2 = self._second_process()
        # 进程 B 看不到可领票据：租约未过期，既不能指定领取也不能排队领取
        self.assertIsNone(queue2.claim("tk_1"))
        self.assertIsNone(queue2.claim())
        self.assertEqual(queue2.stats()[STATUS_CLAIMED], 1)

    def test_expired_lease_is_taken_over_after_crash(self):
        self.queue.enqueue(_ticket())
        first = self.queue.claim("tk_1")
        self.assertEqual(first.attempts, 1)
        _, queue2 = self._second_process()
        self.now[0] += 6.0  # 进程 A 崩溃，租约过期
        second = queue2.claim("tk_1")
        self.assertIsNotNone(second)
        self.assertEqual(second.attempts, 2)
        # 旧持有者被 fencing 拒绝：不能覆盖新持有者的结果
        with self.assertRaises(LeaseLost):
            self.queue.complete(first, candidate_id="cand_old")
        with self.assertRaises(LeaseLost):
            self.queue.release(first)
        # 新持有者完成并保留候选身份
        queue2.complete(second, candidate_id="candidate_002")
        db3, queue3 = self._second_process()
        row = queue3.get("tk_1")
        self.assertEqual(row["status"], STATUS_CONSUMED)
        self.assertEqual(row["candidate_id"], "candidate_002")
        self.assertTrue(row["ticket"].consumed)

    def test_recover_stale_returns_ticket_to_pending(self):
        self.queue.enqueue(_ticket())
        claim = self.queue.claim("tk_1")
        self.now[0] += 6.0
        self.assertEqual(self.queue.recover_stale(), 1)
        self.assertEqual(self.queue.get("tk_1")["status"], STATUS_PENDING)
        with self.assertRaises(LeaseLost):
            self.queue.complete(claim)
        again = self.queue.claim("tk_1")
        self.assertIsNotNone(again)
        self.assertEqual(again.attempts, 2)

    def test_released_ticket_is_claimable_again(self):
        self.queue.enqueue(_ticket())
        claim = self.queue.claim("tk_1")
        self.queue.release(claim, reason="提案失败")
        self.assertEqual(self.queue.get("tk_1")["status"], STATUS_PENDING)
        retry = self.queue.claim("tk_1")
        self.assertEqual(retry.attempts, 2)
        self.queue.complete(retry, candidate_id="candidate_003")
        # 已消费票据不会被重新登记激活
        self.assertFalse(self.queue.enqueue(_ticket()))
        self.assertEqual(self.queue.claim("tk_1"), None)


class TestIsolatedCandidateHost(unittest.TestCase):
    def setUp(self):
        self.repo = AssetRepository(sqlite3.connect(":memory:"))
        self.host = IsolatedCandidateHost(self.repo)

    def _register(self, files, *, entry="main.py", asset_id="pack.cand",
                  version="1.0.0", lifecycle="candidate"):
        return self.repo.register(
            asset_id=asset_id, asset_type="capability_pack",
            namespace="shared_validated", version=version,
            content={"entry": entry, "files": dict(files)},
            lifecycle_state=lifecycle)

    def test_runs_registered_candidate_asset(self):
        asset = self._register({"main.py": "print('candidate-ok')"})
        result = self.host.run(asset.ref)
        self.assertEqual(result["status"], "passed", result)
        self.assertIn("candidate-ok", result["stdout"])
        self.assertEqual(result["asset_ref"], str(asset.ref))
        self.assertEqual(result["content_digest"], asset.content_digest)
        self.assertEqual(result["entry"], "main.py")
        self.assertEqual(result["violations"], [])

    def test_multifile_candidate_imports_sibling_module(self):
        asset = self._register({
            "main.py": "from helper import value\nprint(value)",
            "helper.py": "value = 'sibling-import-ok'"})
        result = self.host.run(asset.ref)
        self.assertEqual(result["status"], "passed", result)
        self.assertIn("sibling-import-ok", result["stdout"])

    def test_timeout_kills_candidate_asset(self):
        asset = self._register({"main.py": "import time\ntime.sleep(5)"})
        result = self.host.run(asset.ref, timeout=0.5)
        self.assertEqual(result["status"], "timeout")
        self.assertLess(result["duration_s"], 3.0)

    def test_network_is_blocked_inside_candidate_asset(self):
        asset = self._register(
            {"main.py": "import socket\nsocket.getaddrinfo('localhost', 80)"})
        result = self.host.run(asset.ref)
        self.assertEqual(result["status"], "violation")
        self.assertTrue(any("socket" in item for item in result["violations"]))

    def test_credentials_are_not_inherited(self):
        os.environ["EVOAGENT_API_KEY"] = "should-not-leak"
        try:
            asset = self._register(
                {"main.py": "import os\nprint(os.environ.get('EVOAGENT_API_KEY'))"})
            result = self.host.run(asset.ref)
        finally:
            os.environ.pop("EVOAGENT_API_KEY", None)
        self.assertEqual(result["status"], "passed")
        self.assertNotIn("should-not-leak", result["stdout"] + result["stderr"])

    def test_asset_without_files_is_rejected(self):
        asset = self.repo.register(
            asset_id="pack.no-code", asset_type="knowledge",
            namespace="shared_validated", version="1.0.0",
            content={"text": "不是代码"}, lifecycle_state="candidate")
        with self.assertRaises(CandidateAssetError):
            self.host.run(asset.ref)

    def test_missing_entry_is_rejected(self):
        asset = self._register({"other.py": "print(1)"})
        with self.assertRaises(CandidateAssetError):
            self.host.run(asset.ref)

    def test_tampered_content_is_rejected_before_execution(self):
        asset = self._register({"main.py": "print('legit')"})
        forged = ('{"entry": "main.py", "files": {"main.py": '
                  '"print(\'tampered\')"}}')
        self.repo._conn.execute(
            "UPDATE assets SET content_json=? WHERE asset_id=? AND version=?",
            (forged, asset.asset_id, asset.version))
        self.repo._conn.commit()
        with self.assertRaises(CandidateAssetError):
            self.host.run(asset.ref)

    def test_path_escape_is_rejected(self):
        asset = self._register({"main.py": "print('x')",
                                "../escape.py": "print('escape')"})
        with self.assertRaises(ValueError):
            self.host.run(asset.ref)


class TestControllerQueueIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_queue_controller_")
        self.path = os.path.join(self.tmp, "app.db")
        self.db = Database(self.path)

    def tearDown(self):
        self.db._conn.close()

    def test_register_problem_persists_and_rehydrates(self):
        controller = EvolutionController(store=self.db)
        self.assertTrue(controller.register_problem(_ticket()))
        self.assertFalse(controller.register_problem(_ticket()))
        reopened = Database(self.path)
        self.addCleanup(reopened._conn.close)
        self.assertEqual(ProblemQueue(reopened).pending()[0]["ticket"].ticket_id,
                         "tk_1")
        # 新进程装载未消费票据，不丢证据
        restarted = EvolutionController(store=reopened)
        self.assertEqual([t.ticket_id for t in restarted.tickets], ["tk_1"])

    def test_signal_pipeline_consumes_ticket_and_keeps_candidate_identity(self):
        controller = EvolutionController(store=self.db)
        controller.register_problem(_ticket())
        proposed = controller.run_signal_pipeline()
        self.assertEqual(len(proposed), 1)
        candidate_id = proposed[0]
        row = ProblemQueue(self.db).get("tk_1")
        self.assertEqual(row["status"], STATUS_CONSUMED)
        self.assertEqual(row["candidate_id"], candidate_id)
        # 重启后：队列不重复投递，候选身份仍可解析
        reopened = Database(self.path)
        self.addCleanup(reopened._conn.close)
        restarted = EvolutionController(store=reopened)
        self.assertEqual(list(restarted.tickets), [])
        self.assertEqual(restarted.run_signal_pipeline(), [])
        self.assertIsNotNone(reopened.get_candidate(candidate_id))
        self.assertEqual(restarted.status_of(candidate_id).value,
                         "CANDIDATE_CREATED")


if __name__ == "__main__":
    unittest.main()
