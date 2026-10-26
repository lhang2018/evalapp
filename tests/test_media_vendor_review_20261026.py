"""Synthetic transport probes; no real vendor or billable request."""
import base64
from dataclasses import replace

import pytest

from evoagent.tools.media_vendor import (MediaVendorClient, MediaVendorConfig,
                                         MediaVendorError, MediaVendorRefused)


def config(**changes):
    return replace(MediaVendorConfig(provider="fixture", base_url="http://fixture.invalid",
        api_key="synthetic", model_version="fixture-v1", budget_usd=1,
        price_per_second_usd=0.1), **changes)


def receipt(**changes):
    return dict(asset_id="synthetic-asset", content_b64=base64.b64encode(b"fixture").decode(),
                vendor_cost_usd=0.1, **changes)


@pytest.mark.parametrize("field,value", [("budget_usd", float("inf")),
    ("price_per_second_usd", float("inf")), ("edit_price_usd", float("nan")),
    ("timeout_s", float("inf")), ("edit_price_usd", -1)])
def test_invalid_config_never_calls_transport(field, value):
    calls = []
    client = MediaVendorClient(config(**{field: value}),
        transport=lambda *args: calls.append(args) or receipt())
    with pytest.raises(MediaVendorRefused):
        client.render_clip("fixture", duration_s=1, actor="review", reason="probe")
    assert calls == []


@pytest.mark.parametrize("cost", [float("nan"), float("inf"), "0.1", True])
def test_invalid_receipt_cost_fails_closed(cost):
    row = receipt()
    row["vendor_cost_usd"] = cost
    client = MediaVendorClient(config(), transport=lambda *args: row)
    with pytest.raises(MediaVendorError):
        client.render_clip("fixture", duration_s=1, actor="review", reason="probe")
    assert client.spent_usd == 0
    assert client.report()["reserved_usd"] == 0.1
    assert client.failures[-1]["refunded"] is False


def test_edit_rejects_missing_asset_id():
    row = receipt()
    row["asset_id"] = ""
    client = MediaVendorClient(config(), transport=lambda *args: row)
    with pytest.raises(MediaVendorError):
        client.edit_clip("source", ["trim"], actor="review", reason="probe")


def test_failure_is_not_a_refund_and_needs_explicit_billing_reconciliation():
    calls = []

    def transport(*args):
        calls.append(args)
        if len(calls) == 1:
            raise MediaVendorError("synthetic timeout after send")
        return receipt()

    client = MediaVendorClient(config(), transport=transport)
    with pytest.raises(MediaVendorError):
        client.render_clip("fixture", duration_s=1, actor="review", reason="probe")
    failure = client.failures[-1]
    assert failure["refunded"] is False
    assert failure["cost_status"] == "unknown"
    assert client.report()["reserved_usd"] == 0.1
    with pytest.raises(MediaVendorRefused):
        client.render_clip("fixture", duration_s=1, actor="review", reason="retry")
    assert len(calls) == 1
    client.reconcile_failure(0, actual_cost_usd=0.08, billing_ref="fixture-bill-1",
                             actor="review", reason="synthetic provider bill")
    assert client.spent_usd == 0.08
    assert client.report()["reserved_usd"] == 0
    assert client.failures[0]["cost_status"] == "reconciled"
    # Idempotent replay cannot erase a charge or add a second charge.
    client.reconcile_failure(0, actual_cost_usd=0.08, billing_ref="fixture-bill-1",
                             actor="review", reason="replay")
    with pytest.raises(MediaVendorError):
        client.reconcile_failure(0, actual_cost_usd=0, billing_ref="other",
                                 actor="review", reason="conflict")
    result = client.render_clip("fixture", duration_s=1, actor="review", reason="retry")
    assert result["ok"] and client.spent_usd == 0.18


def test_unexpected_transport_failure_still_blocks_and_reconciles_real_charge():
    def transport(*args):
        raise TimeoutError("synthetic timeout")

    client = MediaVendorClient(config(), transport=transport)
    with pytest.raises(MediaVendorError):
        client.render_clip("fixture", duration_s=1, actor="review", reason="probe")
    assert client.report()["unknown_costs"] == 1
    with pytest.raises(MediaVendorError):
        client.reconcile_failure(0, actual_cost_usd=float("nan"), billing_ref="bad",
                                 actor="review", reason="probe")
    assert client.report()["reserved_usd"] == 0.1
    client.reconcile_failure(0, actual_cost_usd=1.5, billing_ref="over-budget-fixture",
                             actor="review", reason="synthetic bill")
    assert client.spent_usd == 1.5
    assert client.report()["remaining_usd"] == -0.5
    with pytest.raises(MediaVendorRefused):
        client.render_clip("fixture", duration_s=1, actor="review", reason="probe")
