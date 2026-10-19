"""工具契约/注册表/权限测试（PRD 14-16, 70 / Arch 6）。"""

import tempfile
import unittest
from pathlib import Path

from evoagent.core.permissions import (PermissionController,
                                       PermissionDecision, ToolPolicy)
from evoagent.tools.builtin import calculate_tool, file_write_tool
from evoagent.tools.contract import class_error, ToolContract, ToolResult, validate_against_schema
from evoagent.tools.registry import ToolRegistry


def fresh_registry(perms=None, workdir: str | None = None,
                   file_write_runner=None) -> ToolRegistry:
    """构造测试工具注册表；file_write 使用隔离工作目录避免污染 cwd。"""
    reg = ToolRegistry(perms)
    c, r = calculate_tool()
    reg.register(c, r)
    c, r = file_write_tool(workdir=workdir or tempfile.mktemp())
    reg.register(c, r)
    return reg


class TestTool(unittest.TestCase):
    def test_contract_validation_rejects_bad_args(self):
        reg = fresh_registry()
        out = reg.execute("calculate", {})
        self.assertFalse(out.ok)
        self.assertEqual(out.error_class, "invalid_arguments")

    def test_successful_execution_independent_verification(self):
        reg = fresh_registry()
        out = reg.execute("calculate", {"expression": "1+2"})
        self.assertTrue(out.ok)
        self.assertEqual(out.data, 3)
        self.assertTrue(out.independently_verified)
        self.assertEqual(out.evidence["tool"], "calculate")
        self.assertTrue(out.idempotency_key)

    def test_unknown_tool(self):
        reg = fresh_registry()
        out = reg.execute("nope", {})
        self.assertEqual(out.error_class, "tool_not_found")

    def test_high_risk_needs_confirmation(self):
        wd = tempfile.mktemp()
        reg = fresh_registry(PermissionController(high_risk_mode="confirm"),
                             workdir=wd)
        out = reg.execute("file_write", {"path": "a.txt", "content": "x"})
        self.assertFalse(out.ok)
        self.assertEqual(out.error_class, "auth_or_permission")
        # 确认后放行（独立验证重新读取比对）
        reg2 = fresh_registry(PermissionController(
            high_risk_mode="confirm",
            confirm_callback=lambda op, args: True), workdir=wd)
        out2 = reg2.execute("file_write", {"path": "sub/a.txt", "content": "x"})
        self.assertTrue(out2.ok)
        self.assertEqual(out2.evidence["tool"], "file_write")
        self.assertTrue(Path(wd, "sub", "a.txt").exists())

    def test_schema_validator(self):
        schema = {"type": "object", "required": ["a"],
                  "properties": {"a": {"type": "number"}}}
        self.assertIsNone(validate_against_schema({"a": 1}, schema))
        self.assertIn("缺少必填", validate_against_schema({}, schema))
        sub = {"type": "object",
               "properties": {"a": {"type": "number"}}}
        err = validate_against_schema({"a": "str"}, sub)
        self.assertIn("类型不符", err)

    def test_error_classification(self):
        self.assertEqual(class_error(TimeoutError()), "timeout")
        self.assertEqual(class_error(ValueError()), "invalid_arguments")

    def test_tool_policy_explicit_deny(self):
        perms = PermissionController()
        perms.register_tool_policy(ToolPolicy(tool_id="calculate",
                                              decision=PermissionDecision.DENY))
        reg = fresh_registry(perms)
        out = reg.execute("calculate", {"expression": "1"})
        self.assertEqual(out.error_class, "auth_or_permission")


if __name__ == "__main__":
    unittest.main()
