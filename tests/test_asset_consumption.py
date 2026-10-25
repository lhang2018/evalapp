# -*- coding: utf-8 -*-
"""ALN-06-R：执行步的精确资产版本消费绑定。

验收口径：
- 实际执行绑定的专家/工作流携带精确 version + content_digest；
- retired/draft 不可被新运行绑定（执行前拒绝）；
- 旧 run 绑定保持固定：恢复/续跑只验证存在与摘要，资产事后退役不断链；
- 工作流库/专家注册表内容与绑定资产摘要不一致 = 库漂移，拒绝执行；
- 未注入 binder 时 controller 行为与既有完全一致。
"""

import shutil
import tempfile
import unittest

import evoagent.core.task as T
from evoagent.app import App
from evoagent.assets.consumption import (
    AssetBindingMissing, AssetNotConsumable, ConsumptionError,
    RuntimeAssetBinder,
)
from evoagent.assets.mapping import AssetDraft, expert_to_asset, workflow_to_asset
from evoagent.assets.repository import AssetRepository, repository_for
from evoagent.experts.registry import Expert
from evoagent.workflows.models import WorkflowDef, WorkflowNode


def promote(repo, asset):
    """candidate → validating → approved → active（带证据与来源）。"""
    for state in ("validating", "approved", "active"):
        asset = repo.transition(asset.asset_id, asset.version, state,
                                evidence_refs=("eval/consumption",))
    return asset


class BinderTestBase(unittest.TestCase):
    def setUp(self):
        import sqlite3
        self.repo = AssetRepository(sqlite3.connect(":memory:"))
        self.binder = RuntimeAssetBinder(self.repo)
        self.wf = WorkflowDef(
            workflow_id="wf_bind", version="1.0.0",
            nodes=[WorkflowNode(node_id="a", tool="calculate",
                                args={"expression": "3+4"})])
        self.wf_asset = promote(self.repo,
                                self.repo.register_draft(
                                    workflow_to_asset(self.wf)))

    def tearDown(self):
        try:
            self.repo._conn.close()
        except Exception:
            pass


class TestBindExactVersions(BinderTestBase):
    def test_bind_active_workflow_records_ref_digest_and_source(self):
        bindings = self.binder.bind("workflow", "wf_bind")
        ref = "workflow:wf_bind@1.0.0"
        self.assertIn(ref, bindings)
        record = bindings[ref]
        self.assertEqual(record["content_digest"],
                         self.wf_asset.content_digest)
        self.assertEqual(record["selected_from"], "active")
        self.assertEqual(record["asset_type"], "workflow")

    def test_bind_expert_includes_dependency_closure(self):
        skill = promote(self.repo, self.repo.register_draft(AssetDraft(
            asset_id="skill:calc_skill", asset_type="skill",
            namespace="shared_validated", version="1.0.0",
            content={"x": 1}, source_refs=("skills/calc",))))
        promote(self.repo, self.repo.register_draft(expert_to_asset(
            Expert(expert_id="general", quality_metrics=["acc"],
                   dependencies=["calc_skill"]),
            dependency_versions={"calc_skill": "1.0.0"})))
        bindings = self.binder.bind("expert", "general")
        self.assertEqual(
            sorted(bindings),
            ["expert:general@1.0.0", "skill:calc_skill@1.0.0"])

    def test_retired_asset_cannot_bind_new_run(self):
        self.repo.transition(self.wf_asset.asset_id, self.wf_asset.version,
                             "retired")
        with self.assertRaises(AssetNotConsumable):
            self.binder.bind("workflow", "wf_bind")

    def test_candidate_only_asset_not_consumable(self):
        self.repo.register_draft(workflow_to_asset(WorkflowDef(
            workflow_id="wf_cand", version="0.1.0",
            nodes=[WorkflowNode(node_id="a", tool="calculate")])))
        with self.assertRaises(AssetNotConsumable):
            self.binder.bind("workflow", "wf_cand")

    def test_unregistered_asset_reports_missing(self):
        with self.assertRaises(AssetBindingMissing):
            self.binder.bind("workflow", "wf_never")

    def test_prefer_ref_binds_exact_and_rejects_mismatch(self):
        promote(self.repo, self.repo.register_draft(workflow_to_asset(
            WorkflowDef(workflow_id="wf_multi", version="0.9.0",
                        nodes=[WorkflowNode(node_id="a",
                                            tool="calculate")]))))
        bindings = self.binder.bind(
            "workflow", "wf_multi", prefer_ref="workflow:wf_multi@0.9.0")
        self.assertIn("workflow:wf_multi@0.9.0", bindings)
        with self.assertRaises(ConsumptionError):
            self.binder.bind("workflow", "wf_multi",
                             prefer_ref="workflow:other@0.9.0")

    def test_multiple_active_versions_pick_highest_deterministically(self):
        for version in ("1.0.0", "1.2.0", "1.10.0"):
            promote(self.repo, self.repo.register_draft(workflow_to_asset(
                WorkflowDef(workflow_id="wf_v", version=version,
                            nodes=[WorkflowNode(node_id="a",
                                                tool="calculate")]))))
        bindings = self.binder.bind("workflow", "wf_v")
        self.assertIn("workflow:wf_v@1.10.0", bindings)
        self.assertEqual(bindings["workflow:wf_v@1.10.0"]["selected_from"],
                         "active")


class TestPinnedBindings(BinderTestBase):
    def setUp(self):
        super().setUp()
        self.bindings = self.binder.bind("workflow", "wf_bind")

    def test_verify_pinned_ignores_later_retirement(self):
        """旧 run 版本固定：资产事后退役不得阻断已绑定它的运行。"""
        self.repo.transition(self.wf_asset.asset_id, self.wf_asset.version,
                             "retired")
        self.binder.verify_pinned(self.bindings)

    def test_verify_pinned_rejects_digest_drift(self):
        import copy
        tampered = copy.deepcopy(self.bindings)
        ref = next(iter(tampered))
        tampered[ref]["content_digest"] = "0" * 64
        with self.assertRaises(ConsumptionError):
            self.binder.verify_pinned(tampered)

    def test_verify_pinned_rejects_collected_asset(self):
        self.repo.transition(self.wf_asset.asset_id, self.wf_asset.version,
                             "deprecated")
        self.repo.transition(self.wf_asset.asset_id, self.wf_asset.version,
                             "retired")
        self.assertTrue(self.repo.collect(self.wf_asset.asset_id,
                                          self.wf_asset.version))
        with self.assertRaises(AssetBindingMissing):
            self.binder.verify_pinned(self.bindings)


class ControllerAssetBindingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_06r_")
        self.app = App(data_dir=self.tmp)
        self.wf = WorkflowDef(
            workflow_id="wf_06r", version="1.2.0", name="精确消费测试流",
            nodes=[
                WorkflowNode(node_id="a", tool="calculate",
                             args={"expression": "3+4"}),
                WorkflowNode(node_id="b", tool="calculate",
                             args={"expression": "5*6"}),
            ])
        self.app._workflows["wf_06r"] = self.wf
        self.repo = repository_for(self.app.db)
        self.binder = RuntimeAssetBinder(self.repo)
        self.app.controller.asset_binder = self.binder
        # 注入 binder 的部署必须专家也资产化（fail-closed：未登记即拒）；
        # 必须用 App 实际专家对象注册——注册内容与执行时注册表内容一致
        promote(self.repo, self.repo.register_draft(expert_to_asset(
            self.app.experts.get("general"))))

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _register_active_asset(self, wf=None):
        target = wf or self.wf
        return promote(self.repo,
                       self.repo.register_draft(workflow_to_asset(target)))

    def _new_task(self, task_id="t_06r"):
        task = T.Task(task_id=task_id, user_goal="精确版本消费")
        task.workflow_ref = "wf_06r"
        self.app.db.save_task(task)
        return task

    def test_execute_binds_exact_version_and_freezes_in_manifest(self):
        self._register_active_asset()
        out = self.app.controller.execute(self._new_task())
        self.assertEqual(out.task_status.value, "SUCCEEDED",
                         out.failure_reason)
        bindings = (out.runtime_manifest or {}).get("asset_bindings") or {}
        ref = "workflow:wf_06r@1.2.0"
        self.assertIn(ref, bindings)
        self.assertTrue(bindings[ref]["content_digest"])
        self.assertIn("asset_bindings", out.version_manifest)
        events = [e for e in self.app.bus.query()
                  if e.event_type == "asset_bindings_frozen"]
        self.assertTrue(events, "冻结绑定应发出事件")

    def test_execute_rejects_retired_asset_before_side_effects(self):
        asset = self._register_active_asset()
        self.repo.transition(asset.asset_id, asset.version, "retired")
        out = self.app.controller.execute(self._new_task("t_06r_ret"))
        self.assertEqual(out.task_status.value, "FAILED")
        self.assertIn("资产消费校验失败", out.failure_reason or "")
        self.assertNotIn("node_runs", out.runtime_manifest or {},
                         "retired 拒绝必须发生在任何节点执行之前")

    def test_execute_rejects_library_drift(self):
        """工作流库内容漂移（同版本不同正文）与绑定资产摘要不符即拒绝。"""
        self._register_active_asset()
        self.wf.nodes[0].args = {"expression": "999*999"}
        out = self.app.controller.execute(self._new_task("t_06r_drift"))
        self.assertEqual(out.task_status.value, "FAILED")
        self.assertIn("摘要不一致", out.failure_reason or "")

    def test_resume_path_verifies_pinned_without_rebinding(self):
        self._register_active_asset()
        out = self.app.controller.execute(self._new_task())
        bindings = out.runtime_manifest["asset_bindings"]
        # 资产退役后，续跑绑定校验仍通过（旧 run 版本固定）
        ref = next(iter(bindings))
        asset_id, version = ref.split("@")
        self.repo.transition(asset_id, version, "retired")
        self.app.controller._bind_task_assets(
            out, expert=None, workflow=self.wf)
        # 库漂移在续跑路径同样拒绝
        self.wf.nodes[0].args = {"expression": "1*1"}
        with self.assertRaises(ConsumptionError):
            self.app.controller._bind_task_assets(
                out, expert=None, workflow=self.wf)

    def test_controller_without_binder_unchanged(self):
        self.app.controller.asset_binder = None
        out = self.app.controller.execute(self._new_task("t_06r_nobind"))
        self.assertEqual(out.task_status.value, "SUCCEEDED",
                         out.failure_reason)
        self.assertNotIn("asset_bindings", out.runtime_manifest or {})


if __name__ == "__main__":
    unittest.main()
