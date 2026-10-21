# -*- coding: utf-8 -*-
"""E-07 四类信号 → 问题票据 → 自动候选提案（GAP 第二批第 7 项）。

四类信号（失败/成本异常/质量退化/能力缺口）契约与判据：
core/contracts.py EVOLUTION_SIGNAL_SPEC（代码侧单点补写）。
链路：真实运行事件 → SignalCollector（判据 + 冷却去重）→ ProblemTicket
→ run_signal_pipeline → propose_candidate（晋级仍由门禁独立裁决）。
"""

import shutil
import tempfile
import unittest

from evoagent.app import App
from evoagent.acceptance.verify import Verdict
from evoagent.core import contracts as C
from evoagent.core.task import Task
from evoagent.evolution.signals import SignalCollector, SignalThresholds
from evoagent.tools.contract import ToolResult


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="evoagent_esig_")
        self.app = App(data_dir=self.tmp)
        self.evolution = self.app.evolution
        self.collector = self.app.signal_collector

    def tearDown(self):
        self.app.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestSignalContract(Base):
    def test_four_signal_spec_complete(self):
        self.assertEqual(len(C.EVOLUTION_SIGNAL_TYPES), 4)
        for st in C.EVOLUTION_SIGNAL_TYPES:
            spec = C.EVOLUTION_SIGNAL_SPEC[st]
            for key in ("hook_event", "trigger", "inputs", "criteria",
                        "cooldown_seconds", "change_scope"):
                self.assertTrue(spec.get(key), f"{st}.{key}")
        # 冷却表覆盖全部类型
        self.assertEqual(set(C.EVOLUTION_SIGNAL_COOLDOWN_SPEC),
                         set(C.EVOLUTION_SIGNAL_TYPES))

    def test_target_component_naming_rule(self):
        import re
        pat = re.compile(C.EVOLUTION_SIGNAL_TARGET_PATTERN)
        for good in ("expert/photo_edit", "tool/http_get", "router",
                     "budget", "release/1.0.0"):
            self.assertTrue(pat.match(good), good)
        for bad in ("expert/", "budget/extra", "release", "expert/a/b"):
            self.assertFalse(pat.match(bad), bad)


class TestFailureSignal(Base):
    def test_diagnosed_case_creates_ticket_and_candidate(self):
        task = Task(user_goal="导出相册", domain="photo_edit")
        self.app.db.save_task(task)
        self.app.diagnostics.record_failure(
            task, Verdict(passed=False, checks=[]),
            [ToolResult(ok=False, error="timeout error",
                        error_class="timeout", submitted=True, executed=True,
                        contract_valid=True, side_effect_uncertain=False,
                        evidence={"tool": "http_get"})],
            stop_reason="")
        signal_tickets = [t for t in self.evolution.tickets
                          if t.signal_type == C.EVOLUTION_SIGNAL_FAILURE]
        self.assertEqual(len(signal_tickets), 1)
        ticket = signal_tickets[0]
        self.assertEqual(ticket.target_component, "expert/photo_edit")
        self.assertTrue(any(e.startswith("diagnostic_case/")
                            for e in ticket.evidence))
        # 消费链：票据 → 自动候选（证据映射 + 命名规范）
        proposed = self.evolution.run_signal_pipeline()
        self.assertEqual(len(proposed), 1)
        cand = self.evolution.candidates[proposed[0]]
        self.assertEqual(cand.target_component, "expert/photo_edit")
        self.assertTrue(cand.root_cause_evidence)
        self.assertEqual(cand.change_scope, "local")
        # 已消费票据出队，不再残留
        self.assertFalse([t for t in self.evolution.tickets
                          if t.signal_type and t.candidate_id])

    def test_same_target_within_cooldown_deduplicated(self):
        for i in range(2):
            task = Task(user_goal=f"导出相册{i}", domain="photo_edit")
            self.app.db.save_task(task)
            self.app.diagnostics.record_failure(
                task, Verdict(passed=False, checks=[]), [], stop_reason="")
        tickets = [t for t in self.evolution.tickets
                   if t.signal_type == C.EVOLUTION_SIGNAL_FAILURE]
        self.assertEqual(len(tickets), 1, "冷却窗口内同目标只出一票")
        # 冷却清零后允许再次出票
        self.collector.thresholds.cooldown_seconds[
            C.EVOLUTION_SIGNAL_FAILURE] = 0
        task = Task(user_goal="导出相册3", domain="photo_edit")
        self.app.db.save_task(task)
        self.app.diagnostics.record_failure(
            task, Verdict(passed=False, checks=[]), [], stop_reason="")
        tickets = [t for t in self.evolution.tickets
                   if t.signal_type == C.EVOLUTION_SIGNAL_FAILURE]
        self.assertEqual(len(tickets), 2)


class TestCostAnomalySignal(Base):
    def _task_with_cost(self, tid: str, cost: float) -> Task:
        task = Task(task_id=tid, user_goal="移动相册")
        task.result_evidence = [
            {"budget": {"usage": {"cost": cost, "model_calls": 3,
                                   "tool_calls": 2}}}]
        return task

    def test_insufficient_evidence_no_signal_then_anomaly(self):
        col = self.collector
        # 近窗样本不足 → 证据不足不出信号（不假设）
        for i in range(C.COST_ANOMALY_MIN_RECENT):
            self.assertIsNone(col.inspect_task_cost(
                self._task_with_cost(f"t_c{i}", 0.01)))
        self.assertEqual(len(col._cost_history), C.COST_ANOMALY_MIN_RECENT)
        # 异常任务：成本超近窗中位数×判据倍数与绝对下限
        ticket = col.inspect_task_cost(self._task_with_cost("t_hot", 0.5))
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.target_component, "budget")
        self.assertIn("cost=0.5000", " ".join(ticket.evidence))
        self.assertEqual(ticket.change_scope, "budget_policy")

    def test_pipeline_proposes_budget_candidate(self):
        col = self.collector
        for i in range(C.COST_ANOMALY_MIN_RECENT):
            col.inspect_task_cost(self._task_with_cost(f"t_p{i}", 0.01))
        col.inspect_task_cost(self._task_with_cost("t_hot", 0.5))
        proposed = self.evolution.run_signal_pipeline()
        self.assertEqual(len(proposed), 1)
        cand = self.evolution.candidates[proposed[0]]
        self.assertEqual(cand.target_component, "budget")
        self.assertEqual(cand.change_scope, "budget_policy")


class TestQualityRegressionSignal(Base):
    def test_monitor_drop_creates_ticket_and_throttles_open_component(self):
        cand = self.evolution.propose_candidate(
            "1.0.0", "expert/recommendation", "p", "h")
        self.evolution.candidates[cand.candidate_id].status = \
            cand.status  # 保持未决态
        self.app.bus.emit(event_type="evolution_monitored",
                          component_id="evolution",
                          payload={"candidate_id": cand.candidate_id,
                                   "decision": "rollback_recommended",
                                   "reasons": ["成功率较前窗口下降: 0.15"],
                                   "warnings": []})
        tickets = [t for t in self.evolution.tickets if t.signal_type ==
                   C.EVOLUTION_SIGNAL_QUALITY_REGRESSION]
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0].target_component, "expert/recommendation")
        # 组件级节流：已有未决候选时不重复自动提案
        self.assertEqual(self.evolution.run_signal_pipeline(), [])


class TestCapabilityGapSignal(Base):
    def test_repeated_fallback_creates_expert_gap_candidate(self):
        for i in range(3):
            task = Task(task_id=f"t_gap{i}", user_goal="合成量子插槽清单",
                        intent="fusion_design", domain="fusion_widget")
            self.app.db.save_task(task)
            # hint 短路分类：显式意图即可复现「无专属专家兜底」
            self.app.controller.execute(
                task, intent_hint="fusion_design",
                domain_hint="fusion_widget")
        falls = [e for e in self.app.bus.query()
                 if e.event_type == "expert_fallback"]
        self.assertGreaterEqual(len(falls), 3)
        for e in falls:
            self.assertEqual(e.payload["intent"], "fusion_design")
        gap_tickets = [t for t in self.evolution.tickets if t.signal_type ==
                       C.EVOLUTION_SIGNAL_CAPABILITY_GAP]
        self.assertEqual(len(gap_tickets), 1, "窗口计数达阈值出一票")
        self.assertEqual(gap_tickets[0].target_component,
                         "expert/fusion_design")
        proposed = self.evolution.run_signal_pipeline()
        self.assertEqual(len(proposed), 1)
        cand = self.evolution.candidates[proposed[0]]
        self.assertEqual(cand.target_component, "expert/fusion_design")
        self.assertEqual(cand.change_scope, "experts")


class TestCollectorEdgeCases(Base):
    def test_missing_task_and_unknown_events_ignored(self):
        col = SignalCollector(self.evolution, task_loader=lambda _: None)
        self.assertIsNone(col.inspect_task_cost(None))
        class _Fake:
            event_type = "unrelated"
            payload = {}
            task_id = None
        self.assertIsNone(col.on_event(_Fake()))

    def test_cost_thresholds_covered_by_override(self):
        th = SignalThresholds(cost_ratio=100.0, cooldown_seconds={
            t: 0 for t in C.EVOLUTION_SIGNAL_TYPES})
        self.assertEqual(th.cost_ratio, 100.0)


if __name__ == "__main__":
    unittest.main()
