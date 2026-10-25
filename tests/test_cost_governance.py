"""ALN-08-P：价格档案、代表性任务集与费用对账测试。

验收口径：价格有来源/有效期；未知只计数不造金额；计量与账单差异可解释。
"""

import time

import pytest

from evoagent.core.persistence import Database
from evoagent.models.billing import (BillLine, MeteringLine, reconcile)
from evoagent.models.pricing import (PriceCatalog, PriceEntry, PriceError,
                                     STATUS_FREE, STATUS_PRICED,
                                     UNKNOWN_EXPIRED, UNKNOWN_NO_PRICE,
                                     UNKNOWN_NOT_EFFECTIVE)
from evoagent.models.tasksets import (TaskSample, TaskSetError,
                                      RepresentativeTaskSet, build_sample_set)


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "cost.db"))
    yield database
    database.close()


def price(**overrides):
    values = dict(price_id="pr_1", model_id="model-a", input_per_1k=0.5,
                  output_per_1k=1.5, currency="USD", source_ref="vendor:pricing/2026-10",
                  effective_from=1000.0, effective_to=2000.0)
    values.update(overrides)
    return PriceEntry(**values)


def sample(case_id="c1", goal="查一下明天北京的天气", intent="chat",
           domain="general", scenario="s1"):
    return TaskSample(case_id=case_id, goal=goal, intent=intent,
                      domain=domain, scenario=scenario,
                      tags=("representative",))


# ---------------- 价格档案 ----------------

def test_price_requires_source_and_valid_window():
    with pytest.raises(PriceError):
        price(source_ref="").validate()
    with pytest.raises(PriceError):
        price(effective_to=1000.0).validate()
    with pytest.raises(PriceError):
        price(input_per_1k=-1).validate()
    with pytest.raises(PriceError):
        price(currency="US").validate()


def test_price_resolution_respects_validity_and_never_fabricates(db):
    catalog = PriceCatalog(db)
    catalog.register(price())
    inside = catalog.cost("model-a", 2000, 1000, at=1500.0)
    assert inside["status"] == STATUS_PRICED
    assert inside["cost"] == pytest.approx(2.5)      # (2000*0.5 + 1000*1.5)/1000
    assert inside["source_ref"] == "vendor:pricing/2026-10"
    before = catalog.cost("model-a", 2000, 1000, at=900.0)
    assert before["cost"] is None and before["status"] == UNKNOWN_NOT_EFFECTIVE
    after = catalog.cost("model-a", 2000, 1000, at=2000.0)
    assert after["cost"] is None and after["status"] == UNKNOWN_EXPIRED
    other = catalog.cost("model-b", 2000, 1000, at=1500.0)
    assert other["cost"] is None and other["status"] == UNKNOWN_NO_PRICE


def test_free_price_is_explicit_zero_not_unknown(db):
    catalog = PriceCatalog(db)
    catalog.register(price(price_id="pr_free", model_id="model-free",
                           input_per_1k=0.0, output_per_1k=0.0))
    resolved = catalog.cost("model-free", 100, 50, at=1500.0)
    assert resolved["status"] == STATUS_FREE and resolved["cost"] == 0.0


def test_overlapping_price_windows_rejected_but_adjacent_allowed(db):
    catalog = PriceCatalog(db)
    catalog.register(price())
    with pytest.raises(PriceError):
        catalog.register(price(price_id="pr_2", effective_from=1500.0,
                               effective_to=2500.0))
    catalog.register(price(price_id="pr_3", effective_from=2000.0,
                           effective_to=3000.0))
    catalog.register(price(price_id="pr_eur", currency="EUR"))
    with pytest.raises(PriceError):
        catalog.register(price(effective_from=1000.0, effective_to=2000.0,
                               note="changed"))


def test_unknown_costs_are_counted_never_priced(db):
    catalog = PriceCatalog(db)
    catalog.register(price())
    catalog.register(price(price_id="pr_free", model_id="model-free",
                           input_per_1k=0.0, output_per_1k=0.0))
    rows = [
        {"request_ref": "r1", "model_id": "model-a", "input_tokens": 1000,
         "output_tokens": 1000},
        {"request_ref": "r2", "model_id": "model-free", "input_tokens": 10,
         "output_tokens": 10},
        {"request_ref": "r3", "model_id": "model-unknown", "input_tokens": 10,
         "output_tokens": 10},
        {"request_ref": "r4", "model_id": "model-a", "input_tokens": None,
         "output_tokens": 10},
    ]
    summary = catalog.aggregate(rows, at=1500.0)
    assert summary["priced_count"] == 1
    assert summary["free_count"] == 1
    assert summary["unknown_count"] == 2
    assert summary["amount"] == pytest.approx(2.0)
    assert summary["unknown_amount"] is None      # 未知费用没有金额，不填 0
    assert summary["by_status"] == {"priced": 1, "free": 1,
                                    "unknown:no_price": 1,
                                    "unknown:invalid_usage": 1}


def test_price_catalog_survives_restart(tmp_path):
    path = str(tmp_path / "restart.db")
    first = Database(path)
    try:
        PriceCatalog(first).register(price())
    finally:
        first.close()
    reopened = Database(path)
    try:
        catalog = PriceCatalog(reopened)
        assert [entry.price_id for entry in catalog.entries()] == ["pr_1"]
        assert catalog.cost("model-a", 1000, 0, at=1500.0)["cost"] == pytest.approx(0.5)
    finally:
        reopened.close()


# ---------------- 费用对账 ----------------

def metering(*rows):
    return [MeteringLine(**row) for row in rows]


def bill(*rows):
    return [BillLine(**row) for row in rows]


def test_reconcile_matched_and_rounding_are_explained(db):
    result = reconcile(
        metering({"request_ref": "r1", "model_id": "m", "amount": 1.0},
                 {"request_ref": "r2", "model_id": "m", "amount": 2.0}),
        bill({"request_ref": "r1", "model_id": "m", "amount": 1.0},
             {"request_ref": "r2", "model_id": "m", "amount": 2.004}),
        period="2026-10")
    report = result.to_dict()
    assert report["passed"] is True
    assert report["explanations"] == {"matched": 1, "rounding": 1}
    assert report["followups"] == []
    assert report["metered_known_total"] == pytest.approx(3.0)
    assert report["billed_total"] == pytest.approx(3.004)
    assert report["delta"] == pytest.approx(0.004)


def test_reconcile_missing_and_bill_only_lines_become_followups(db):
    result = reconcile(
        metering({"request_ref": "r1", "model_id": "m", "amount": 1.0}),
        bill({"request_ref": "r2", "model_id": "m", "amount": 2.0}),
        period="2026-10")
    report = result.to_dict()
    assert report["passed"] is False
    assert report["explanations"] == {"invoice_missing_line": 1, "bill_only_line": 1}
    assert {item["reason"] for item in report["followups"]} == {
        "invoice_missing_line", "bill_only_line"}
    assert report["delta"] == pytest.approx(1.0)


def test_reconcile_unknown_price_is_counted_without_amount(db):
    result = reconcile(
        metering({"request_ref": "r1", "model_id": "m", "amount": None,
                  "status": "unknown:no_price"}),
        bill({"request_ref": "r1", "model_id": "m", "amount": 9.99}),
        period="2026-10")
    report = result.to_dict()
    assert report["unknown_count"] == 1
    assert report["unknown_amount"] is None
    assert report["followups"] == [{"request_ref": "r1", "reason": "unknown_price"}]
    assert report["rows"][0]["metered_amount"] is None
    assert report["passed"] is False


def test_reconcile_currency_and_amount_mismatch_need_followup(db):
    result = reconcile(
        metering({"request_ref": "r1", "model_id": "m", "amount": 1.0},
                 {"request_ref": "r2", "model_id": "m", "amount": 5.0}),
        bill({"request_ref": "r1", "model_id": "m", "amount": 1.0,
              "currency": "EUR"},
             {"request_ref": "r2", "model_id": "m", "amount": 7.5}),
        period="2026-10")
    report = result.to_dict()
    assert report["passed"] is False
    assert {"currency_mismatch", "amount_mismatch"} <= set(report["explanations"])
    assert len(report["followups"]) == 2


def test_reconcile_duplicate_bill_lines_are_flagged(db):
    result = reconcile(
        metering({"request_ref": "r1", "model_id": "m", "amount": 1.0}),
        bill({"request_ref": "r1", "model_id": "m", "amount": 1.0},
             {"request_ref": "r1", "model_id": "m", "amount": 1.0}),
        period="2026-10")
    report = result.to_dict()
    assert report["passed"] is False
    assert report["explanations"] == {"duplicate_bill_line": 1}


# ---------------- 代表性任务集 ----------------

def test_task_set_requires_provenance_anonymization_and_clean_samples():
    valid = build_sample_set(set_id="ts1", version="1.0",
                             source_ref="sanitized:ticket-batch-42",
                             samples=[sample("c1", domain="general"),
                                      sample("c2", intent="travel",
                                             domain="travel")])
    coverage = valid.coverage()
    assert coverage["total"] == 2
    assert coverage["by_domain"] == {"general": 1, "travel": 1}
    assert "finance" in coverage["missing_domains"]
    with pytest.raises(TaskSetError):
        build_sample_set(set_id="ts2", version="1.0", source_ref=" ",
                         samples=[sample()])
    with pytest.raises(TaskSetError):
        RepresentativeTaskSet(set_id="ts3", version="1.0",
                              source_ref="sanitized:batch", collected_at=time.time(),
                              anonymized=False, anonymization_method="none",
                              samples=[sample()]).validate()
    with pytest.raises(TaskSetError):
        build_sample_set(set_id="ts4", version="1.0", source_ref="sanitized:batch",
                         samples=[sample("c1", goal="重置我的 password 并发送 token")])
    with pytest.raises(TaskSetError):
        build_sample_set(set_id="ts5", version="1.0", source_ref="sanitized:batch",
                         samples=[sample("dup"), sample("dup")])


def test_task_set_snapshot_roundtrip(db):
    task_set = build_sample_set(set_id="ts1", version="1.0",
                                source_ref="sanitized:ticket-batch-42",
                                samples=[sample("c1"), sample("c2", domain="travel")],
                                collected_at=1234.0)
    task_set.save(db)
    loaded = RepresentativeTaskSet.load(db, "ts1", "1.0")
    assert loaded.to_dict() == task_set.to_dict()
    with pytest.raises(TaskSetError):
        RepresentativeTaskSet.load(db, "ts1", "9.9")
