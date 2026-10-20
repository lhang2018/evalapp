"""进化候选生命周期 / 评测门禁 / 发布管理测试（PRD 48-54, 59-63 / Arch 10.4, 11, 13）。"""

import unittest

from evoagent.evolution.candidates import (CandidateStatus,
                                           EvolutionCandidate,
                                           IllegalCandidateTransition)
from evoagent.evolution.controller import EvolutionController, PUBLISH_RULES
from evoagent.evolution.evaluator import (EvalCase, EvalDataset, Evaluator,
                                          GateThresholds, ReleaseGate)


class FakeRunner:
    """评测用假任务执行器（确定性：同一 case 在两次运行中行为一致）。"""

    def __init__(self, ok_rate: float):
        self.ok_rate = ok_rate

    def run_case(self, case):
        # 注意：不能用 hash(string)——PYTHONHASHSEED 会让结果不确定
        rank = (int(str(case.case_id), 36) % 10) / 10
        return rank < self.ok_rate, 0.05, {"cost": 0.01}


class TestCandidateLifecycle(unittest.TestCase):
    def _cand(self) -> EvolutionCandidate:
        return EvolutionCandidate(candidate_id="c1", baseline_version="v0",
                                  target_component="expert/travel",
                                  target_problem="路由错误")

    def test_full_lifecycle_to_promoted(self):
        c = self._cand()
        c.transition(CandidateStatus.TRIAGED, "r1")
        c.transition(CandidateStatus.HYPOTHESIZED, "r2")
        c.transition(CandidateStatus.CANDIDATE_CREATED, "r3")
        c.transition(CandidateStatus.TESTING, "r4")
        c.transition(CandidateStatus.EVALUATING, "r5")
        c.transition(CandidateStatus.APPROVAL_PENDING, "r6")
        c.transition(CandidateStatus.RELEASE_READY, "r7")
        c.transition(CandidateStatus.DEPLOYING, "r8")
        c.transition(CandidateStatus.MONITORING, "r9")
        c.transition(CandidateStatus.PROMOTED, "r10")
        self.assertEqual(len(c.transition_log), 10)
        self.assertEqual(c.benefit_report(), {"status": "not_evaluated"})

    def test_illegal_transition_rejected(self):
        c = self._cand()
        with self.assertRaises(IllegalCandidateTransition):
            c.transition(CandidateStatus.DEPLOYING, "skip everything")

    def test_terminal_locked(self):
        c = self._cand()
        c.transition(CandidateStatus.TRIAGED, "r")
        c.transition(CandidateStatus.REJECTED, "不通过")
        with self.assertRaises(IllegalCandidateTransition):
            c.transition(CandidateStatus.TESTING, "复活")


class TestGate(unittest.TestCase):
    def _ds(self, ids):
        return EvalDataset(dataset_id="ds1", cases=[
            EvalCase(case_id=i, input_goal="g") for i in ids])

    def test_gate_pass(self):
        ev = Evaluator(FakeRunner(0.95))
        ids = [str(i) for i in range(10)]
        base = ev.run(self._ds(ids), baseline=True)
        cand = ev.run(self._ds(ids), baseline=False)
        self.assertAlmostEqual(base.success_rate, cand.success_rate, places=6)
        gate = ReleaseGate().judge(base, cand,
                                   thresholds=GateThresholds(min_success_rate=0.8),
                                   safety_ok=True)
        self.assertTrue(gate["passed"], gate["reasons"])

    def test_gate_reject_on_low_success_and_safety_veto(self):
        ev = Evaluator(FakeRunner(0.35))
        ids = [str(i) for i in range(10)]
        base = ev.run(self._ds(ids), baseline=True)
        cand = ev.run(self._ds(ids), baseline=False)
        self.assertLess(cand.success_rate, 0.8)
        gate = ReleaseGate().judge(base, cand,
                                   thresholds=GateThresholds(min_success_rate=0.8),
                                   safety_ok=True)
        self.assertFalse(gate["passed"])
        # 一票否决：即使其他指标都好，安全失败不可用平均分抵消（Arch 11.5）
        ev2 = Evaluator(FakeRunner(0.99))
        b2 = ev2.run(self._ds(ids), baseline=True)
        c2 = ev2.run(self._ds(ids), baseline=False)
        gate2 = ReleaseGate().judge(b2, c2, safety_ok=False)
        self.assertFalse(gate2["passed"])
        self.assertTrue(any("安全" in r for r in gate2["reasons"]))


class TestEvolutionController(unittest.TestCase):
    def test_propose_candidate_recorded(self):
        ec = EvolutionController()
        cand = ec.propose_candidate("v0", "expert/travel", "路由错误",
                                    "增加意图白名单", change_scope="expert")
        self.assertEqual(cand.status, CandidateStatus.CANDIDATE_CREATED)
        self.assertEqual(ec.status_of(cand.candidate_id), CandidateStatus.CANDIDATE_CREATED)
        self.assertTrue(cand.rollback_version == "v0")  # PRD 49 必须有回滚版本

    def test_publish_rules(self):
        self.assertEqual(PUBLISH_RULES["L4"], "manual_only")
        self.assertEqual(PUBLISH_RULES["L1"], "auto_gate")


if __name__ == "__main__":
    unittest.main()
