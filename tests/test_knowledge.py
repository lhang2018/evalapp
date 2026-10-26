# -*- coding: utf-8 -*-
"""ALN-27-A 故障经验知识库与跨专家迁移验证。

负向优先：不可追溯的知识被拒、不适用条目不注入、错误经验可撤销、
命中率不能代替任务质量、退化必须可见。
"""

import unittest

from evoagent.knowledge import (
    KnowledgeEntry,
    KnowledgeError,
    KnowledgeStore,
    TaskOutcome,
    evaluate_transfer,
    inapplicability_reason,
    plan_injection,
)


def entry(
    entry_id="k1",
    *,
    key="retry-on-timeout",
    content="超时后先对账再重试",
    source="diag-2026-10-20",
    confidence=0.8,
    domains=("coding",),
    version=1,
    created_at=1000.0,
):
    return KnowledgeEntry(
        entry_id=entry_id,
        key=key,
        content=content,
        source=source,
        confidence=confidence,
        applicable_domains=tuple(domains),
        version=version,
        created_at=created_at,
    )


def outcomes(prefix, expert, passed, failed):
    # The before/after run label is not a logical task identity. Keep the same
    # fixed task manifest while varying outcomes, rather than comparing sets
    # whose IDs depend on whether they passed or which run produced them.
    return [TaskOutcome(task_id=f"{expert}-case-{i}", expert=expert, passed=i < passed)
            for i in range(passed + failed)]


class KnowledgeEntryValidationTest(unittest.TestCase):
    def test_source_is_mandatory(self):
        with self.assertRaises(KnowledgeError):
            entry(source="   ").validate()

    def test_confidence_must_be_in_range(self):
        for bad in (-0.1, 1.1):
            with self.assertRaises(KnowledgeError):
                entry(confidence=bad).validate()

    def test_domains_must_not_be_empty(self):
        with self.assertRaises(KnowledgeError):
            entry(domains=()).validate()

    def test_version_must_be_positive(self):
        with self.assertRaises(KnowledgeError):
            entry(version=0).validate()


class KnowledgeStoreTest(unittest.TestCase):
    def test_duplicate_entry_id_rejected(self):
        store = KnowledgeStore()
        store.add(entry("k1"))
        with self.assertRaises(KnowledgeError):
            store.add(entry("k1"))

    def test_version_must_increase_per_key(self):
        store = KnowledgeStore()
        store.add(entry("k1", version=2))
        with self.assertRaises(KnowledgeError):
            store.add(entry("k2", version=2))
        store.add(entry("k2", version=3))  # 递增允许

    def test_versions_and_latest(self):
        store = KnowledgeStore()
        store.add(entry("v1", version=1))
        store.add(entry("v2", version=2))
        self.assertEqual([e.version for e in store.versions_of("retry-on-timeout")], [1, 2])
        self.assertEqual(store.latest("retry-on-timeout").version, 2)

    def test_revoke_is_idempotent_and_removes_from_active(self):
        store = KnowledgeStore()
        store.add(entry("k1"))
        revoked = store.revoke("k1", reason="结论被推翻", at=2000.0)
        self.assertTrue(revoked.revoked)
        self.assertEqual(store.active(), ())
        again = store.revoke("k1", reason="重复", at=3000.0)
        self.assertEqual(again.revoked_at, 2000.0)  # 不覆盖首次撤销时点

    def test_revoked_latest_falls_back_but_revoked_never_injected(self):
        store = KnowledgeStore()
        store.add(entry("v1", version=1))
        store.add(entry("v2", version=2))
        store.revoke("v2", reason="错误经验", at=2000.0)
        # 撤销最新版等同回归回滚：latest 回退到仍然有效的旧版本，但被撤销的那条
        # 永不注入（见 plan_injection）。
        latest = store.latest("retry-on-timeout")
        self.assertIsNotNone(latest)
        self.assertEqual(latest.version, 1)
        plan = plan_injection(store, domain="coding")
        self.assertEqual([e.entry_id for e in plan.injected], ["v1"])

    def test_revoke_missing_entry_rejected(self):
        with self.assertRaises(KnowledgeError):
            KnowledgeStore().revoke("nope", reason="x", at=1.0)


class ApplicabilityTest(unittest.TestCase):
    def test_reason_reports_cause_or_none(self):
        good = entry()
        self.assertIsNone(inapplicability_reason(good, domain="coding"))
        self.assertIn("适用域", inapplicability_reason(good, domain="finance"))
        self.assertIn("可信度", inapplicability_reason(good, domain="coding", min_confidence=0.9))
        store = KnowledgeStore()
        store.add(entry("k1"))
        store.revoke("k1", reason="撤销", at=2.0)
        self.assertEqual(inapplicability_reason(store.get("k1"), domain="coding"), "已撤销")

    def test_inapplicable_entries_are_never_injected(self):
        store = KnowledgeStore()
        store.add(entry("coding", key="rule-coding", domains=("coding",), confidence=0.9))
        store.add(entry("k2", key="rule-finance", domains=("finance",), confidence=0.95))
        store.add(entry("k3", key="rule-weak", domains=("coding",), confidence=0.2))
        plan = plan_injection(store, domain="coding", min_confidence=0.5)
        injected = {e.entry_id for e in plan.injected}
        self.assertEqual(injected, {"coding"})
        reasons = {s.entry_id: s.reason for s in plan.skipped}
        self.assertIn("适用域", reasons["k2"])
        self.assertIn("可信度", reasons["k3"])

    def test_injected_ordering_and_revocation_effect(self):
        store = KnowledgeStore()
        store.add(entry("k1", key="rule-a", domains=("coding",), confidence=0.6))
        store.add(entry("k2", key="rule-b", domains=("coding",), confidence=0.95))
        plan = plan_injection(store, domain="coding")
        self.assertEqual([e.entry_id for e in plan.injected], ["k2", "k1"])
        # 撤销后不再注入，且带原因
        store.revoke("k2", reason="错误经验", at=3.0)
        plan2 = plan_injection(store, domain="coding")
        self.assertEqual([e.entry_id for e in plan2.injected], ["k1"])
        self.assertEqual(
            next(s.reason for s in plan2.skipped if s.entry_id == "k2"), "已撤销"
        )


class TransferTest(unittest.TestCase):
    def test_improvement_requires_task_quality(self):
        before = outcomes("b", "coding", 2, 2)
        after = outcomes("a", "coding", 4, 0)
        report = evaluate_transfer(before, after)
        self.assertTrue(report.sufficient)
        self.assertEqual(report.verdict, "improved")
        self.assertEqual(report.basis, "task_quality")
        self.assertEqual(report.overall_before, 0.5)
        self.assertEqual(report.overall_after, 1.0)

    def test_unchanged_when_no_difference(self):
        report = evaluate_transfer(
            outcomes("b", "coding", 3, 1), outcomes("a", "coding", 3, 1)
        )
        self.assertEqual(report.verdict, "unchanged")
        self.assertEqual(report.deltas[0].delta, 0.0)

    def test_regression_is_visible_per_expert(self):
        before = outcomes("b", "coding", 4, 0) + outcomes("b", "writer", 2, 2)
        after = outcomes("a", "coding", 4, 0) + outcomes("a", "writer", 0, 4)
        report = evaluate_transfer(before, after)
        self.assertEqual(report.verdict, "degraded")
        self.assertEqual(report.degradations, ("writer",))

    def test_mismatched_expert_sets_cannot_be_attributed(self):
        report = evaluate_transfer(
            outcomes("b", "coding", 3, 1), outcomes("a", "writer", 3, 1)
        )
        self.assertFalse(report.sufficient)
        self.assertEqual(report.verdict, "unproven")

    def test_missing_side_is_unproven(self):
        self.assertEqual(evaluate_transfer([], outcomes("a", "coding", 1, 0)).verdict, "unproven")
        self.assertEqual(evaluate_transfer(outcomes("b", "coding", 1, 0), []).verdict, "unproven")

    def test_hit_rate_alone_cannot_prove_benefit(self):
        report = evaluate_transfer([], [], hit_rate=0.99)
        self.assertFalse(report.sufficient)
        self.assertEqual(report.verdict, "unproven")
        self.assertTrue(any("命中率" in r for r in report.reasons))

    def test_hit_rate_is_ignored_when_task_outcomes_exist(self):
        report = evaluate_transfer(
            outcomes("b", "coding", 2, 2),
            outcomes("a", "coding", 4, 0),
            hit_rate=0.1,
        )
        self.assertEqual(report.verdict, "improved")
        self.assertTrue(any("命中率" in r for r in report.reasons))

    def test_task_outcome_requires_expert(self):
        with self.assertRaises(ValueError):
            TaskOutcome(task_id="t1", expert="  ", passed=True).validate()


if __name__ == "__main__":
    unittest.main()
