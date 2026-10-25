"""ALN-29-B 首发冻结范围与发布门禁：校验器判别力逐条覆盖。

每条用例都构造一个“应该被拒绝”的状态，验证校验器确实拒绝它。
只校验“不该通过”的被拦住，不校验“该通过的都通过”——后者由结构完整性用例保证。
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "check_release_gates.py"
REPO_EVIDENCE = ROOT / "docs/协作开发/验收/首发门禁证据_2026-10-25.json"
REPO_BOARD = ROOT / "docs/协作开发/workboard.json"


def run_cli(evidence, board, *extra):
    with tempfile.TemporaryDirectory(prefix="aln29b-") as tmp:
        evidence_path = Path(tmp) / "evidence.json"
        board_path = Path(tmp) / "board.json"
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
        board_path.write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--evidence", str(evidence_path),
             "--board", str(board_path), *extra],
            capture_output=True, text=True, encoding="utf-8", cwd=str(tmp),
        )
    return completed


def load_repo():
    evidence = json.loads(REPO_EVIDENCE.read_text(encoding="utf-8"))
    board = json.loads(REPO_BOARD.read_text(encoding="utf-8"))
    return evidence, board


def find(items, item_id):
    for item in items:
        if item.get("id") == item_id:
            return item
    raise AssertionError(f"未找到 {item_id}")


class ReleaseGateValidatorTest(unittest.TestCase):
    def test_repo_evidence_table_is_structurally_valid(self):
        """仓库内证据表必须自洽：G0—G7 与 R01—R13 齐全、状态合法。"""
        evidence, board = load_repo()
        completed = run_cli(evidence, board)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("结构与自洽校验通过", completed.stdout)
        self.assertIn("不可宣布首发可用", completed.stdout)

    def test_untested_gate_is_not_counted_as_passed(self):
        """未运行的门禁不得因其他指标提升而被计为通过。

        隔离要求：其余七道门禁与十三项范围全部置为通过且已发布，
        只留 G0 未测试。若“未测试”不阻止发布，本用例必须失败。
        """
        evidence, board = load_repo()
        board["baseline"]["published_commit"] = "b" * 40
        for gate in evidence["gates"]:
            gate["status"] = "pass"
            gate["evidence"] = [{"kind": "command", "ref": "cmd", "result": "ok", "at": "2026-10-25"}]
        for item in evidence["frozen_range"]["required"]:
            item["status"] = "pass"
            item["evidence"] = [{"kind": "command", "ref": "cmd", "result": "ok", "at": "2026-10-25"}]
        find(evidence["gates"], "G0")["status"] = "untested"
        find(evidence["gates"], "G0")["evidence"] = []
        completed = run_cli(evidence, board, "--require-release")
        self.assertEqual(completed.returncode, 2, completed.stdout)
        self.assertIn("不可宣布首发可用", completed.stdout)
        self.assertIn("门禁未测试：G0", completed.stdout)

    def test_pass_without_evidence_is_rejected(self):
        """标记 pass 却没有证据 —— 必须被判错。"""
        evidence, board = load_repo()
        find(evidence["gates"], "G1")["status"] = "pass"
        find(evidence["gates"], "G1")["evidence"] = []
        completed = run_cli(evidence, board)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("没有证据", completed.stdout)

    def test_pass_with_incomplete_evidence_is_rejected(self):
        """证据缺 ref 或 result —— 必须被判错。"""
        evidence, board = load_repo()
        find(evidence["gates"], "G1")["status"] = "pass"
        find(evidence["gates"], "G1")["evidence"] = [{"kind": "command", "ref": "", "result": ""}]
        completed = run_cli(evidence, board)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("缺少", completed.stdout)

    def test_untested_with_evidence_is_rejected(self):
        """标记 untested 却挂证据 —— 状态与证据矛盾，必须被判错。"""
        evidence, board = load_repo()
        find(evidence["gates"], "G2")["evidence"] = [
            {"kind": "command", "ref": "pytest tests/test_aln01_authorization.py",
             "result": "12 passed", "at": "2026-10-25"}]
        completed = run_cli(evidence, board)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("状态与证据矛盾", completed.stdout)

    def test_fail_without_reason_is_rejected(self):
        """标记 fail 却不写明原因与失败路径 —— 必须被判错。"""
        evidence, board = load_repo()
        find(evidence["gates"], "G3")["status"] = "fail"
        find(evidence["gates"], "G3")["reason"] = ""
        completed = run_cli(evidence, board)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("reason", completed.stdout)

    def test_missing_gate_is_rejected(self):
        """少一道门禁 —— 必须被判错，且错误信息要指出缺哪一道。"""
        evidence, board = load_repo()
        evidence["gates"] = [gate for gate in evidence["gates"] if gate["id"] != "G5"]
        completed = run_cli(evidence, board)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("G5", completed.stdout)
        self.assertIn("G0—G7", completed.stdout)

    def test_renumbered_gate_is_rejected(self):
        """门禁数量对但编号错（G5 写成 G8）—— 同样必须被判错。

        只数数量不校验编号的实现会漏掉这种错位，因此断言必须指向具体编号。
        """
        evidence, board = load_repo()
        find(evidence["gates"], "G5")["id"] = "G8"
        completed = run_cli(evidence, board)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("G5", completed.stdout)

    def test_missing_range_item_is_rejected(self):
        """首发范围缺项 —— 必须被判错，且错误信息要指出缺哪一项。"""
        evidence, board = load_repo()
        evidence["frozen_range"]["required"] = [
            item for item in evidence["frozen_range"]["required"] if item["id"] != "R07"]
        completed = run_cli(evidence, board)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("R07", completed.stdout)
        self.assertIn("R01—R13", completed.stdout)

    def test_renumbered_range_item_is_rejected(self):
        """范围项数量对但编号错（R07 写成 R14）—— 同样必须被判错。"""
        evidence, board = load_repo()
        find(evidence["frozen_range"]["required"], "R07")["id"] = "R14"
        completed = run_cli(evidence, board)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("R07", completed.stdout)

    def test_g6_pass_without_published_commit_is_rejected(self):
        """没有已发布版本却把 G6 判为通过 —— 与台账事实矛盾，必须被判错。"""
        evidence, board = load_repo()
        board["baseline"]["published_commit"] = None
        find(evidence["gates"], "G6")["status"] = "pass"
        find(evidence["gates"], "G6")["evidence"] = [
            {"kind": "report", "ref": "docs/协作开发/验收/ALN-03-P-*.md", "result": "联验通过",
             "at": "2026-10-25"}]
        completed = run_cli(evidence, board)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("published_commit", completed.stdout)

    def test_hard_blocking_failure_blocks_release(self):
        """硬性门禁失败时，即使其余全通过也不得放行。"""
        evidence, board = load_repo()
        board["baseline"]["published_commit"] = "0" * 40
        for gate in evidence["gates"]:
            gate["status"] = "pass"
            gate["evidence"] = [{"kind": "command", "ref": "cmd", "result": "ok", "at": "2026-10-25"}]
        for item in evidence["frozen_range"]["required"]:
            item["status"] = "pass"
            item["evidence"] = [{"kind": "command", "ref": "cmd", "result": "ok", "at": "2026-10-25"}]
        find(evidence["gates"], "G2")["status"] = "fail"
        find(evidence["gates"], "G2")["reason"] = "未授权高风险动作发生 1 次"
        find(evidence["gates"], "G2")["evidence"] = []
        completed = run_cli(evidence, board, "--require-release")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("硬性门禁失败：G2", completed.stdout)

    def test_all_gates_passed_and_published_means_release_ready(self):
        """八道门禁与十三项范围全部通过且已发布 —— 才允许判定可发布。"""
        evidence, board = load_repo()
        board["baseline"]["published_commit"] = "a" * 40
        for gate in evidence["gates"]:
            gate["status"] = "pass"
            gate["evidence"] = [{"kind": "command", "ref": "cmd", "result": "ok", "at": "2026-10-25"}]
        for item in evidence["frozen_range"]["required"]:
            item["status"] = "pass"
            item["evidence"] = [{"kind": "command", "ref": "cmd", "result": "ok", "at": "2026-10-25"}]
        completed = run_cli(evidence, board, "--require-release")
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("可宣布首发可用", completed.stdout)

    def test_missing_evidence_file_is_reported(self):
        """证据表不存在时明确报错，不静默放行。"""
        with tempfile.TemporaryDirectory(prefix="aln29b-") as tmp:
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--evidence", str(Path(tmp) / "nope.json")],
                capture_output=True, text=True, encoding="utf-8", cwd=str(tmp),
            )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("证据表不存在", completed.stdout)


if __name__ == "__main__":
    unittest.main()
