"""能力状态端点测试（ALN-24-B）。

验证六档投影与原因：ready/permission_required/denied/dependency_missing/
unverified/unregistered；拒绝策略下受保护动作确实不能执行（既有权限语义）。
"""

import unittest

from evoagent.api.capabilities_api import collect, project_capability
from evoagent.app import App
from evoagent.core.permissions import PermissionController, PermissionDecision, ToolPolicy
from evoagent.tools.contract import ToolContract, ToolResult


def make_tool(app, tool_id, *, probe, side_effect="none"):
    contract = ToolContract(
        tool_id=tool_id, description=f"cap {tool_id}",
        input_schema={"type": "object"}, side_effect_level=side_effect,
        health_check=(lambda: probe) if probe is not None else None)
    app.tools.register(contract,
                       lambda args: ToolResult(ok=True, data="x",
                                               submitted=True, executed=True,
                                               contract_valid=True,
                                               goal_reached=True,
                                               independently_verified=True,
                                               evidence={}))


class TestCapabilityStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = None
        self.app = App(data_dir=self.tmp or None)

    def tearDown(self):
        self.app.db.close()
        self.app.memory.close()

    def _collect(self):
        class Ctx:
            pass
        ctx = Ctx()
        ctx.app = self.app
        return collect(ctx)

    def test_six_states_projected_with_reasons(self):
        make_tool(self.app, "cap_ready", probe=True)
        make_tool(self.app, "cap_probe_fail", probe=False)
        make_tool(self.app, "cap_noprobe", probe=None)
        make_tool(self.app, "cap_denied", probe=True)
        self.app.permissions.register_tool_policy(ToolPolicy(
            tool_id="cap_denied", decision=PermissionDecision.DENY, note="测试拒绝"))
        out = self._collect()
        by_id = {c["tool_id"]: c for c in out["capabilities"]}
        self.assertEqual(by_id["cap_ready"]["state"], "ready")
        self.assertEqual(by_id["cap_probe_fail"]["state"], "dependency_missing")
        self.assertIn("fail-closed", by_id["cap_probe_fail"]["reason"])
        self.assertEqual(by_id["cap_noprobe"]["state"], "unverified")
        self.assertEqual(by_id["cap_denied"]["state"], "denied")
        self.assertIn("拒绝", by_id["cap_denied"]["reason"])
        self.assertEqual(out["counts"].get("denied"), 1)
        self.assertEqual(sum(out["counts"].values()), len(out["capabilities"]))

    def test_high_risk_without_policy_requires_permission(self):
        make_tool(self.app, "cap_confirm", probe=True, side_effect="external")
        # external → high 风险 → 无策略时默认 confirm 路径
        out = self._collect()
        by_id = {c["tool_id"]: c for c in out["capabilities"]}
        self.assertEqual(by_id["cap_confirm"]["state"], "permission_required")


    def test_policy_referenced_but_unregistered(self):
        self.app.permissions.register_tool_policy(ToolPolicy(tool_id="ghost_tool"))
        out = self._collect()
        by_id = {c["tool_id"]: c for c in out["capabilities"]}
        self.assertEqual(by_id["ghost_tool"]["state"], "unregistered")

    def test_denied_tool_cannot_execute(self):
        """拒绝/撤销后受保护动作不能执行：既有权限语义保证（本端点如实展示）。"""
        from evoagent.core.permissions import PermissionDecision
        make_tool(self.app, "cap_blocked", probe=True)
        self.app.permissions.register_tool_policy(ToolPolicy(
            tool_id="cap_blocked", decision=PermissionDecision.DENY))
        result = self.app.tools.execute("cap_blocked", {})
        self.assertFalse(result.ok)
        out = self._collect()
        by_id = {c["tool_id"]: c for c in out["capabilities"]}
        self.assertEqual(by_id["cap_blocked"]["state"], "denied")

    def test_project_capability_unregistered(self):
        c = project_capability("x", registered=False, version="",
                               health=None, dry=None, explicit_deny=False,
                               high_risk_mode="confirm", note="",
                               high_risk=False)
        self.assertEqual(c["state"], "unregistered")


if __name__ == "__main__":
    unittest.main()
