"""记忆治理、模型路由评分、诊断趋势与工作流资产库测试。"""

import shutil
import tempfile
import unittest

from evoagent.app import App
from evoagent.evolution.probes import ProbeRunner
from evoagent.memory.context import ContextAssembler, ContextBudget
from evoagent.memory.store import Credibility, MemoryItem, MemoryKind, Scope
from evoagent.models.base import ModelProfile, ModelRegistry
from evoagent.models.router import ModelRouter
from evoagent.tools.builtin import git_read_tool
from evoagent.workflows.models import WorkflowDef


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_mrd_")
        self.app = App(data_dir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestMemoryGovernance(Base):
    def test_record_use_and_stats(self):
        item = MemoryItem(content="用户偏好简短回答",
                          kind=MemoryKind.USER_PREFERENCE,
                          scope=Scope.PROJECT, status=Credibility.VERIFIED,
                          evidence={"source": "user"})
        self.app.memory.write(item)
        self.app.memory.record_use(item.memory_id)
        self.app.memory.record_use(item.memory_id)
        self.assertEqual(self.app.memory.get(item.memory_id).use_count, 2)
        stats = self.app.memory.stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["by_kind"]["user_preference"], 1)

    def test_relations_and_hybrid_search(self):
        a = MemoryItem(content="发布前必须跑完整回归测试",
                       scope=Scope.PROJECT, status=Credibility.VERIFIED,
                       evidence={"x": 1})
        b = MemoryItem(content="回归测试失败时先看失败任务事件",
                       scope=Scope.PROJECT, status=Credibility.CANDIDATE)
        self.app.memory.write(a)
        self.app.memory.write(b)
        self.app.memory.link(a.memory_id, "refines", b.memory_id)
        related = self.app.memory.related(a.memory_id)
        self.assertEqual([m.memory_id for m in related], [b.memory_id])
        hits = self.app.memory.search("回归测试")
        self.assertTrue(hits)
        self.assertIn(a.memory_id, [m.memory_id for m in hits])

    def test_context_stats_and_use_count(self):
        item = MemoryItem(content="用户偏好中文回复",
                          kind=MemoryKind.USER_PREFERENCE,
                          scope=Scope.TASK, scope_key="t1",
                          status=Credibility.VERIFIED, evidence={"x": 1})
        self.app.memory.write(item)
        assembler = ContextAssembler(self.app.memory, ContextBudget(max_chars=6000))
        ctx = assembler.assemble("请用中文回答", scope_key="t1")
        stats = ctx.stats()
        self.assertGreaterEqual(stats["memory_candidates"], 1)
        self.assertEqual(stats["memory_used"], len(ctx.memories))
        self.assertEqual(stats["chars_before"], stats["chars_after"])
        self.assertEqual(self.app.memory.get(item.memory_id).use_count, 1)


class TestRouterScoring(unittest.TestCase):
    def _router(self):
        reg = ModelRegistry()
        reg.register(ModelProfile(model_id="strong", quality_score=0.95,
                                  price_per_1k_input=0.01,
                                  price_per_1k_output=0.03,
                                  avg_latency_s=2.0))
        reg.register(ModelProfile(model_id="cheap", quality_score=0.45,
                                  price_per_1k_input=0.0001,
                                  price_per_1k_output=0.0002,
                                  avg_latency_s=0.3))
        router = ModelRouter(reg, object())
        router.setup(lambda mid: {"model_id": mid})
        return router

    def test_default_route_prefers_quality(self):
        router = self._router()
        self.assertEqual(router.route("general", "general")["model_id"],
                         "strong")

    def test_prefer_cost_route(self):
        router = self._router()
        self.assertEqual(
            router.route("general", "general", prefer_cost=True)["model_id"],
            "cheap")

    def test_rank_is_sorted_by_score(self):
        rank = self._router().rank()
        self.assertEqual(rank[0]["model_id"], "strong")
        self.assertGreater(rank[0]["score"], rank[1]["score"])


class TestDiagnosticsDepth(Base):
    def test_trends_after_failed_task(self):
        t = self.app.submit("现在几点了", intent="time_query",
                            acceptance=[{"type": "tool_failed"}])
        self.assertEqual(t.task_status.value, "FAILED")
        case = self.app.diagnostics.diagnose_task(t.task_id)
        self.assertIsNotNone(case)
        trends = self.app.diagnostics.trends()
        self.assertGreaterEqual(trends["cases"], 1)
        self.assertTrue(trends["by_category"])
        self.assertTrue(trends["by_status"])

    def test_probe_fault_injection(self):
        runner = ProbeRunner(self.app.tools, self.app.memory,
                             self.app.config.diagnostics)
        err = runner.run("calc_roundtrip", fault="error")
        self.assertIsNone(err.passed)
        self.assertEqual(err.evidence["fault_injected"], "error")
        fail = runner.run("calc_roundtrip", fault="fail")
        self.assertFalse(fail.passed)
        self.assertEqual(fail.evidence["fault_injected"], "fail")


class TestWorkflowLibraryAndGit(Base):
    def test_builtin_workflow_is_persisted(self):
        data = self.app.db.get_workflow_def("comic_drama_demo")
        self.assertIsNotNone(data)
        wf = WorkflowDef.from_dict(data)
        self.assertEqual(wf.workflow_id, "comic_drama_demo")
        self.assertEqual(len(wf.nodes), 7)
        self.assertEqual(wf.nodes[3].tool, "mock_video_clip")

    def test_git_read_tool(self):
        import shutil as sh
        if not sh.which("git"):
            self.skipTest("git 不可用")
        contract, run = git_read_tool(workdir=".")
        self.assertEqual(contract.side_effect_level, "none")
        res = run({"action": "log", "limit": 3})
        self.assertTrue(res.ok, res.error)
        self.assertIsInstance(res.data, str)

    def test_git_read_rejects_write_actions(self):
        _contract, run = git_read_tool(workdir=".")
        res = run({"action": "push"})
        self.assertFalse(res.ok)
        self.assertEqual(res.error_class, "auth_or_permission")


if __name__ == "__main__":
    unittest.main()
