"""工具替代（Arch 6.4 / PRD 16）。

主工具可重试失败后允许切到声明过的备用工具，但不得扩大权限或副作用，
且副作用状态不确定时不得切换。
"""

import unittest

from evoagent.tools.contract import ToolContract, ToolResult
from evoagent.tools.registry import ToolRegistry


def _contract(tool_id, *, side_effect="none", permissions=None,
              required=None, substitutes=None):
    return ToolContract(
        tool_id=tool_id,
        input_schema={
            "type": "object",
            "properties": {name: {"type": "string"}
                           for name in (required or [])},
            "required": list(required or []),
        },
        side_effect_level=side_effect,
        permission_requirements=list(permissions or []),
        timeout_policy=0,
        retry_policy=0,
        substitutes=list(substitutes or []),
    )


def _ok(tool_id):
    def runner(_args):
        return ToolResult(ok=True, data={"tool": tool_id}, executed=True,
                          submitted=True, contract_valid=True)
    return runner


class TestToolSubstitution(unittest.TestCase):
    def setUp(self):
        self.reg = ToolRegistry()
        self.calls = []

    def _register(self, contract, runner):
        def wrapped(args):
            self.calls.append(contract.tool_id)
            return runner(args)
        self.reg.register(contract, wrapped)

    def test_fallback_used_after_retryable_failure(self):
        def down(_args):
            raise ConnectionError("service down")
        self._register(_contract("search", required=["query"],
                                 substitutes=["search_mirror"]), down)
        self._register(_contract("search_mirror", required=["query"]),
                       _ok("search_mirror"))
        out = self.reg.execute("search", {"query": "x"})
        self.assertTrue(out.ok)
        self.assertEqual(out.data["tool"], "search_mirror")
        self.assertEqual(out.evidence["substituted_from"], "search")
        self.assertEqual(out.evidence["substitution_attempts"][0]["status"],
                         "ok")
        self.assertEqual(self.calls, ["search", "search_mirror"])

    def test_no_fallback_for_non_retryable_error(self):
        def bad_args(_args):
            raise ValueError("bad arguments")
        self._register(_contract("search", required=["query"],
                                 substitutes=["search_mirror"]), bad_args)
        self._register(_contract("search_mirror", required=["query"]),
                       _ok("search_mirror"))
        out = self.reg.execute("search", {"query": "x"})
        self.assertFalse(out.ok)
        self.assertEqual(out.error_class, "invalid_arguments")
        self.assertNotIn("substitution_attempts", out.evidence)
        self.assertEqual(self.calls, ["search"])

    def test_higher_side_effect_substitute_rejected(self):
        def down(_args):
            raise ConnectionError("service down")
        self._register(_contract("search", required=["query"],
                                 substitutes=["shell_search"]), down)
        self._register(_contract("shell_search", required=["query"],
                                 side_effect="external"), _ok("shell_search"))
        out = self.reg.execute("search", {"query": "x"})
        self.assertFalse(out.ok)
        attempts = out.evidence["substitution_attempts"]
        self.assertEqual(attempts[0]["status"], "rejected")
        self.assertEqual(attempts[0]["reason"], "higher_side_effect")
        self.assertEqual(self.calls, ["search"])

    def test_more_permissions_substitute_rejected(self):
        def down(_args):
            raise ConnectionError("service down")
        self._register(_contract("search", required=["query"],
                                 substitutes=["admin_search"]), down)
        self._register(_contract("admin_search", required=["query"],
                                 permissions=["admin"]), _ok("admin_search"))
        out = self.reg.execute("search", {"query": "x"})
        self.assertFalse(out.ok)
        self.assertEqual(out.evidence["substitution_attempts"][0]["reason"],
                         "more_permissions")
        self.assertEqual(self.calls, ["search"])

    def test_uncertain_side_effect_blocks_substitution(self):
        def down(_args):
            raise ConnectionError("service down")
        self._register(_contract("send", required=["query"],
                                 side_effect="external",
                                 substitutes=["send_backup"]), down)
        self._register(_contract("send_backup", required=["query"],
                                 side_effect="external"), _ok("send_backup"))
        out = self.reg.execute("send", {"query": "x"})
        self.assertFalse(out.ok)
        self.assertTrue(out.side_effect_uncertain)
        self.assertNotIn("substitution_attempts", out.evidence)
        self.assertEqual(self.calls, ["send"])

    def test_missing_or_incompatible_substitute_recorded(self):
        def down(_args):
            raise TimeoutError("timeout")
        self._register(_contract("search", required=["query"],
                                 substitutes=["missing", "no_args"]), down)
        self._register(_contract("no_args", required=[]), _ok("no_args"))
        out = self.reg.execute("search", {"query": "x"})
        self.assertFalse(out.ok)
        attempts = out.evidence["substitution_attempts"]
        self.assertEqual(attempts[0]["status"], "not_registered")
        self.assertEqual(attempts[1]["status"], "rejected")
        self.assertEqual(attempts[1]["reason"], "missing_input:query")
        self.assertEqual(self.calls, ["search"])

    def test_executed_failed_substitute_stops_chain(self):
        def never_sent(_args):
            # 明确未发送：可切换；与 ConnectionError 抛异常（副作用不确定）不同
            return ToolResult(ok=False, error="connect refused",
                              error_class="network_or_unavailable",
                              submitted=True)

        def ran_but_failed(_args):
            return ToolResult(ok=False, error="rejected by peer",
                              error_class="tool_defect_or_env",
                              executed=True, submitted=True)

        self._register(_contract("send", required=["query"],
                                 side_effect="external",
                                 substitutes=["send_backup", "send_backup2"]),
                       never_sent)
        self._register(_contract("send_backup", required=["query"],
                                 side_effect="external"), ran_but_failed)
        self._register(_contract("send_backup2", required=["query"],
                                 side_effect="external"), _ok("send_backup2"))
        out = self.reg.execute("send", {"query": "x"})
        self.assertFalse(out.ok)
        self.assertEqual(self.calls, ["send", "send_backup"])
        self.assertEqual(out.evidence["substitution_attempts"][0]["reason"],
                         "executed")

    def test_failed_substitute_returns_original_failure(self):
        def down(_args):
            raise ConnectionError("primary down")

        def also_down(_args):
            raise ConnectionError("mirror down")
        self._register(_contract("search", required=["query"],
                                 substitutes=["search_mirror"]), down)
        self._register(_contract("search_mirror", required=["query"]),
                       also_down)
        out = self.reg.execute("search", {"query": "x"})
        self.assertFalse(out.ok)
        self.assertEqual(out.error_class, "network_or_unavailable")
        self.assertEqual(len(out.evidence["substitution_attempts"]), 1)
        self.assertEqual(out.evidence["substitution_attempts"][0]["status"],
                         "failed")


if __name__ == "__main__":
    unittest.main()
