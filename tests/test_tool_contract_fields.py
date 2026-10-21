# -*- coding: utf-8 -*-
"""Tool Contract 字段批（GAP ⑤b，契约 §4）。

五个缺口：risk_class（声明式）、offline_capable、requires_network、
cost_model、contract_test_ref。
接线上三点：
1. 注册单点推导 risk_class（未声明按 side_effect_level 推导）
2. 执行裁决声明与执行同源：取声明与副作用级推导的严格者
   （声明只能升高、不能降低——fail-closed）
3. 内置工具显式声明离线/联网与成本模型
"""

import shutil
import tempfile
import unittest

import evoagent.core.task as T
from evoagent.app import App
from evoagent.tools.contract import ToolContract
from evoagent.tools.registry import ToolRegistry


class TestToolContractFields(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_tc4_")
        self.app = App(data_dir=self.tmp)

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fields_roundtrip_builtins(self):
        h = self.app.tools
        for tid in ("current_time", "calculate", "fs_list", "file_read",
                    "file_write", "git_read"):
            c = h.contract(tid)
            self.assertTrue(c.offline_capable, tid)
            self.assertFalse(c.requires_network, tid)
            self.assertTrue(c.cost_model, tid)
            self.assertEqual(c.contract_test_ref, "tests/test_tool_health.py",
                             tid)
        hget = h.contract("http_get")
        self.assertTrue(hget.requires_network)
        self.assertFalse(hget.offline_capable)

    def test_risk_class_derived_at_register(self):
        reg = ToolRegistry()
        reg.register_mock_tool("m_external", lambda a: {"x": 1},
                               side_effect_level="external")
        self.assertEqual(reg.contract("m_external").risk_class, "high")

    def test_declared_risk_lifts_decision_fail_closed(self):
        # 显式声明 critical（注册时给定）但副作用级 none：
        # 声明抬入高风险确认链 → 无回调拒绝
        from evoagent.core.permissions import PermissionController
        perms = PermissionController(high_risk_mode="confirm",
                                     confirm_callback=lambda op, a: False)
        reg = ToolRegistry(permissions=perms)
        reg.register(ToolContract(
            tool_id="declared_crit", risk_class="critical",
            input_schema={"type": "object"}),
            lambda args: ToolResult(ok=True, data={"x": 1},
                                    submitted=True, executed=True,
                                    contract_valid=True))
        result = reg.execute("declared_crit", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.error_class, "auth_or_permission")

    def test_derived_risk_does_not_lift(self):
        # 自动推导（未显式声明）的工具：无高风险名单归属 → 原有行为不变
        from evoagent.core.permissions import PermissionController
        perms = PermissionController(high_risk_mode="confirm",
                                     confirm_callback=lambda op, a: False)
        reg = ToolRegistry(permissions=perms)
        reg.register_mock_tool("derived_ok", lambda a: {"x": 1},
                               side_effect_level="none")
        self.assertEqual(reg.contract("derived_ok").risk_class, "low")
        self.assertFalse(reg.contract("derived_ok").risk_declared)
        result = reg.execute("derived_ok", {})
        self.assertTrue(result.ok)  # 未声明不变更行为面

    def test_declaration_cannot_lower_risk(self):
        # 显式声明 low 但副作用 destructive：不得降低——仍走确认流
        from evoagent.core.permissions import PermissionController
        perms = PermissionController(high_risk_mode="confirm",
                                     confirm_callback=lambda op, a: False)
        reg = ToolRegistry(permissions=perms)
        reg.register(ToolContract(
            tool_id="decl_low", risk_class="low",
            side_effect_level="destructive",
            input_schema={"type": "object"}),
            lambda args: ToolResult(ok=True, data={"x": 1},
                                    submitted=True, executed=True,
                                    contract_valid=True))
        self.assertEqual(reg.contract("decl_low").declared_risk(),
                         T.RiskLevel.LOW)
        result = reg.execute("decl_low", {})
        # 声明不可降低：destructive 依旧高风险 → 无回调拒绝
        self.assertFalse(result.ok)
        self.assertEqual(result.error_class, "auth_or_permission")

    def test_declared_risk_enum_mapping(self):
        c = ToolContract(tool_id="x", risk_class="high")
        self.assertEqual(c.declared_risk(), T.RiskLevel.HIGH)
        self.assertIsNone(ToolContract(tool_id="y").declared_risk())
        self.assertIsNone(
            ToolContract(tool_id="z", risk_class="bogus").declared_risk())


if __name__ == "__main__":
    unittest.main()
