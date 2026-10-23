"""ALN-07-E 副作用账本对账：七条验收逐条覆盖。"""
from __future__ import annotations

import copy
import random
import unittest

from evoagent.planning.planner import ExecutionPlan, PlanStep
from evoagent.planning.reconcile import (
    DeclaredEffect, EffectRecord, ReconciliationError, ReconciliationReport,
    SideEffectReconciler, reconcile_plan,
)


def _plan(steps, *, plan_id: str = "plan_main") -> ExecutionPlan:
    return ExecutionPlan(plan_id=plan_id, goal="带副作用的计划", steps=steps)


def _rows(*specs) -> list:
    """构造账本行；(tool, status) 或 (tool, status, effect_id)。"""
    out = []
    for i, spec in enumerate(specs):
        tool, status = spec[0], spec[1]
        eid = spec[2] if len(spec) > 2 else f"e{i}"
        out.append({"effect_id": eid, "task_id": "t1", "tool_id": tool,
                    "status": status, "idempotency_key": f"k{i}",
                    "effect_summary": "", "run_id": "r1"})
    return out


def _rec(effect_id: str = "e1", tool_id: str = "file_write",
         status: str = "confirmed") -> EffectRecord:
    return EffectRecord(effect_id=effect_id, tool_id=tool_id, status=status)


class ReadOnlyLedger:
    """只暴露只读查询；任何非 history 的属性访问（写入接口）立即失败。"""

    def __init__(self, rows):
        self._rows = list(rows)
        self.reads = []

    def history(self, task_id):
        return list(self._rows)

    def __getattr__(self, name):
        raise AttributeError(f"对账期间调用了非只读接口: {name}")


class TestCompleteAttribution(unittest.TestCase):
    """验收1：每条账本记录都要落到某个计划步骤，或明确归入 undeclared。"""

    def test_every_record_is_accounted(self):
        plan = _plan([
            PlanStep("s1", "写文件", tool_hint="file_write", state="done"),
            PlanStep("s2", "发请求", tool_hint="http_post", state="done"),
        ])
        rows = _rows(("file_write", "confirmed", "e1"),
                     ("http_post", "confirmed", "e2"),
                     ("shell_exec", "confirmed", "e3"),
                     ("db_delete", "unknown", "e4"))
        rep = reconcile_plan(plan, rows, task_id="t1")
        accounted = (len(rep.matched) + len(rep.undeclared)
                     + len(rep.uncertain))
        self.assertEqual(accounted, len(rows),
                         "有账本记录既未归因也未标记为未声明——被静默丢弃")
        self.assertEqual([u.record.effect_id for u in rep.undeclared], ["e3"])
        # uncertain 直接存 EffectRecord（无 UndeclaredEffect 包装层）
        self.assertEqual([r.effect_id for r in rep.uncertain], ["e4"])
        self.assertFalse(rep.ok, "存在未声明执行与未决副作用，ok 不能为 True")

    def test_missing_tool_id_rejected(self):
        plan = _plan([PlanStep("s1", "写", tool_hint="file_write",
                               state="done")])
        with self.assertRaises(ReconciliationError):
            reconcile_plan(plan, [{"effect_id": "e1", "status": "confirmed"}])


class TestUnexecutedNotSuccess(unittest.TestCase):
    """验收2：声称完成但无副作用证据，必须判为未执行且不 ok。"""

    def test_done_step_without_record_is_unexecuted(self):
        plan = _plan([
            PlanStep("s1", "写文件", tool_hint="file_write", state="done"),
            PlanStep("s2", "读文件", tool_hint="file_read", state="done"),
        ])
        rows = _rows(("file_write", "confirmed", "e1"))
        rep = reconcile_plan(plan, rows)
        self.assertEqual([d.step_id for d in rep.unexecuted], ["s2"])
        self.assertFalse(rep.ok, "存在声称完成却无证据的步骤，ok 不能为 True")
        self.assertIn("s2", " ".join(rep.findings()))

    def test_pending_step_declaration_is_not_unexecuted(self):
        """时机未到的步骤不应被算成失真——如实区分。"""
        plan = _plan([
            PlanStep("s1", "做过的", tool_hint="file_write", state="done"),
            PlanStep("s2", "还没轮到", tool_hint="http_post", state="pending"),
        ])
        rows = _rows(("file_write", "confirmed", "e1"))
        rep = reconcile_plan(plan, rows)
        self.assertEqual(rep.unexecuted, [], "待执行步骤被误判为失执行")
        self.assertEqual([d.step_id for d in rep.pending_declarations], ["s2"])

    def test_skipped_step_excluded(self):
        plan = _plan([
            PlanStep("s1", "跳过的", tool_hint="file_write", state="skipped"),
        ])
        rep = reconcile_plan(plan, [])
        self.assertTrue(rep.ok)
        self.assertEqual(rep.declared, [])
        self.assertEqual(rep.unexecuted, [])


class TestUnsettledIsNotCompletion(unittest.TestCase):
    """验收3：未决副作用不得计入 matched（fail-closed）。"""

    def test_unsettled_statuses_never_match(self):
        for status in ("planned", "submitted", "unknown"):
            with self.subTest(status=status):
                plan = _plan([
                    PlanStep("s1", "外部调用", tool_hint="http_post",
                             state="done"),
                ])
                rows = _rows(("http_post", status, "e1"))
                rep = reconcile_plan(plan, rows)
                self.assertEqual(rep.matched, [],
                                 f"{status} 状态的记录被算作已完成")
                self.assertEqual([r.effect_id for r in rep.uncertain], ["e1"])
                self.assertFalse(rep.ok)

    def test_failed_record_is_not_completion(self):
        plan = _plan([
            PlanStep("s1", "写文件", tool_hint="file_write", state="done"),
        ])
        rows = _rows(("file_write", "failed", "e1"))
        rep = reconcile_plan(plan, rows)
        self.assertEqual(rep.matched, [], "失败记录被算作已完成")
        self.assertFalse(rep.ok)


class TestUndeclaredDetected(unittest.TestCase):
    """验收4：账本出现计划未声明的工具即越权/失真。"""

    def test_undeclared_tool_flagged(self):
        plan = _plan([PlanStep("s1", "只读", tool_hint="file_read",
                               state="done")])
        rows = _rows(("file_read", "confirmed", "e1"),
                     ("shell_exec", "confirmed", "e2"))
        rep = reconcile_plan(plan, rows)
        self.assertEqual([u.record.tool_id for u in rep.undeclared],
                         ["shell_exec"])
        self.assertEqual(rep.undeclared[0].reason, "no_matching_declaration")
        self.assertFalse(rep.ok)

    def test_execution_exceeding_declaration_count(self):
        """同一工具计划声明一次却执行两次：第二次必须被标出来，不能吞掉。"""
        plan = _plan([PlanStep("s1", "写一次", tool_hint="file_write",
                               state="done")])
        rows = _rows(("file_write", "confirmed", "e1"),
                     ("file_write", "confirmed", "e2"))
        rep = reconcile_plan(plan, rows)
        self.assertEqual(len(rep.matched), 1)
        self.assertEqual(len(rep.undeclared), 1)
        self.assertEqual(rep.undeclared[0].reason,
                         "exceeds_declaration_count")
        self.assertFalse(rep.ok)


class TestDeterminism(unittest.TestCase):
    """验收5：与输入次序无关。"""

    def test_shuffled_inputs_same_report(self):
        plan = _plan([
            PlanStep("s1", "写文件", tool_hint="file_write", state="done"),
            PlanStep("s2", "发请求", tool_hint="http_post", state="done"),
            PlanStep("s3", "查数据", tool_hint="db_query", state="pending"),
        ])
        rows = _rows(("file_write", "confirmed", "e1"),
                     ("http_post", "unknown", "e2"),
                     ("shell_exec", "confirmed", "e3"))
        base = reconcile_plan(plan, rows, task_id="t1").to_dict()
        rng = random.Random(7)
        for _ in range(12):
            shuffled = copy.deepcopy(rows)
            rng.shuffle(shuffled)
            steps = list(plan.steps)
            rng.shuffle(steps)
            shuffled_plan = _plan(steps)
            got = reconcile_plan(shuffled_plan, shuffled,
                                 task_id="t1").to_dict()
            self.assertEqual(got, base, "对账结论依赖了输入次序")


class TestReadOnly(unittest.TestCase):
    """验收6：纯读，零写入权限。"""

    def test_ledger_entry_only_reads_history(self):
        rows = _rows(("file_write", "confirmed", "e1"))
        plan = _plan([PlanStep("s1", "写", tool_hint="file_write",
                               state="done")])
        ledger = ReadOnlyLedger(rows)
        reconcile = SideEffectReconciler()
        rep = reconcile.reconcile_ledger(plan, ledger, task_id="t1")
        self.assertTrue(rep.ok)
        # ReadOnlyLedger 任何非 history 属性访问都会抛错；走到这里即证明
        # 对账全程没有碰过写入接口。

    def test_reconcile_does_not_mutate_inputs(self):
        plan = _plan([PlanStep("s1", "写", tool_hint="file_write",
                               state="done")])
        rows = _rows(("file_write", "confirmed", "e1"),
                     ("http_post", "unknown", "e2"))
        snapshot_plan = copy.deepcopy(plan.to_dict())
        snapshot_rows = copy.deepcopy(rows)
        reconcile_plan(plan, rows)
        self.assertEqual(plan.to_dict(), snapshot_plan, "对账修改了计划")
        self.assertEqual(rows, snapshot_rows, "对账修改了账本记录")

    def test_no_ledger_reference_in_signatures(self):
        """结构性保证：本模块从未导入账本，物理上无法写入。

        用 AST 检查 import 图，而不是字符串 grep——避免把文档里的词
        或模块自身的方法名（如 SideEffectReconciler.reconcile）误判为
        对账本 reconcile() 的调用。
        """
        import ast
        from evoagent.planning import reconcile as mod
        with open(mod.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                for alias in node.names:
                    imported.add(f"{base}.{alias.name}")
        # 任何路径上都不能引入副作用账本
        offenders = [i for i in imported
                     if "side_effects" in i or "persistence" in i
                     or "SideEffectLedger" in i]
        self.assertEqual(offenders, [],
                         f"对账模块引入了账本/持久化，破坏纯读保证: {offenders}")
        # 也不得出现乐观并发/写入所需的锁与上报通道
        self.assertNotIn("evoagent.core.database", imported)


class TestSerialization(unittest.TestCase):
    """验收7：序列化无损往返。"""

    def test_roundtrip(self):
        plan = _plan([
            PlanStep("s1", "写文件", tool_hint="file_write", state="done"),
            PlanStep("s2", "未执行", tool_hint="db_delete", state="done"),
        ])
        rows = _rows(("file_write", "confirmed", "e1"),
                     ("shell_exec", "confirmed", "e2"),
                     ("http_post", "unknown", "e3"))
        rep = reconcile_plan(plan, rows, task_id="t1")
        restored = ReconciliationReport.from_dict(rep.to_dict())
        self.assertEqual(restored.to_dict(), rep.to_dict())
        self.assertEqual(restored.summary, rep.summary)
        self.assertEqual(restored.ok, rep.ok)
        self.assertEqual(restored.findings(), rep.findings())

    def test_summary_counts_match_lists(self):
        plan = _plan([
            PlanStep("s1", "写", tool_hint="file_write", state="done"),
            PlanStep("s2", "未做", tool_hint="db_delete", state="done"),
        ])
        rows = _rows(("file_write", "confirmed", "e1"),
                     ("shell", "confirmed", "e2"),
                     ("http", "unknown", "e3"))
        rep = reconcile_plan(plan, rows)
        s = rep.summary
        self.assertEqual(s["declared"], len(rep.declared))
        self.assertEqual(s["matched"], len(rep.matched))
        self.assertEqual(s["undeclared"], len(rep.undeclared))
        self.assertEqual(s["unexecuted"], len(rep.unexecuted))
        self.assertEqual(s["uncertain"], len(rep.uncertain))


class TestConsistentCase(unittest.TestCase):
    """护栏：完全一致的计划与账本应判 ok，避免套件只测了拒绝路径。"""

    def test_clean_reconciliation_ok(self):
        plan = _plan([
            PlanStep("s1", "写文件", tool_hint="file_write", state="done"),
            PlanStep("s2", "发请求", tool_hint="http_post", state="done"),
        ])
        rows = _rows(("file_write", "confirmed", "e1"),
                     ("http_post", "confirmed", "e2"))
        rep = reconcile_plan(plan, rows, task_id="t1")
        self.assertTrue(rep.ok, "一致场景未能判 ok——成功路径缺失")
        self.assertEqual(rep.findings(), [])
        self.assertEqual(len(rep.matched), 2)
        self.assertTrue(rep.divergence_free)


if __name__ == "__main__":
    unittest.main()
