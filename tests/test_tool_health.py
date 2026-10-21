# -*- coding: utf-8 -*-
"""工具健康探针（GAP 死字段修复①：health_check 恒真 → 真探针三值语义）。

R-08 原状：builtin 工具零设置 health_check，registry.health() 对无探针
工具恒返回 True —— 监控面 tools_health 永远全绿，健康信息为零。
新口径：True/False 来自真探针；None=未声明（如网络类，不廉价探测）→
unknown；探针抛错 fail-closed 计 False。
"""

import shutil
import tempfile
import unittest
from unittest import mock

from evoagent.app import App
from evoagent.tools.builtin import calculate_tool, file_write_tool, \
    http_get_tool
from evoagent.tools.contract import ToolContract


class TestToolProbes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_f04h_")
        self.app = App(data_dir=self.tmp)

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_probe_reports_unknown_not_true(self):
        # http_get 未声明探针 → None（unknown），不得恒真
        self.assertIsNone(self.app.tools.health()["http_get"])

    def test_calculate_probe_true(self):
        self.assertIs(self.app.tools.health()["calculate"], True)

    def test_local_time_probe_true(self):
        self.assertIs(self.app.tools.health()["current_time"], True)

    def test_fs_probe_true(self):
        self.assertIs(self.app.tools.health()["fs_list"], True)

    def test_file_write_probe_writable_workdir(self):
        self.assertIs(self.app.tools.health()["file_write"], True)

    def test_git_probe(self):
        st = self.app.tools.health()["git_read"]
        self.assertIn(st, (True, False))  # 依环境有无 git，但必是真的判定

    def test_broken_probe_fail_closed(self):
        contract, runner = calculate_tool()
        contract.health_check = lambda: 1 / 0  # 探针炸了
        self.app.tools.register(contract, runner)
        self.assertIs(self.app.tools.health()["calculate"], False)

    def test_health_shape_has_no_forced_true(self):
        h = self.app.tools.health()
        # 不再出现"全恒真"：dict 中存在 None（未声明）或探测值
        self.assertTrue(any(v is None for v in h.values())
                        or len(set(h.values())) > 1)

    def test_field_defaults_none_contract_locked(self):
        # 产生路径锁定：默认无探针声明时是显式 None（unknown），非 True
        self.assertIsNone(ToolContract(tool_id="x").health_check)
        # file_write 工厂的真实探针可调用
        contract, _ = file_write_tool(self.tmp)
        self.assertTrue(callable(contract.health_check))
        self.assertTrue(contract.health_check())


if __name__ == "__main__":
    unittest.main()
