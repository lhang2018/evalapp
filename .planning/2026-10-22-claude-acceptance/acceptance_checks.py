"""Independent acceptance checks; run explicitly against a clean reviewed checkout.

These assert required behavior and intentionally fail where acceptance is blocked.
They do not patch production code or enter the normal tests/ baseline count.
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path.cwd()))

from evoagent.app import App
from evoagent.assets.entity import AssetConflict
from evoagent.assets.repository import AssetRepository
from evoagent.models.base import Action, ModelProfile
from evoagent.models.metering import (COST_ESTIMATE, TOKEN_ESTIMATE,
                                     MeteredModel, ModelUsage, UsageLedger)

OBSERVATIONS = {}


def observe(name, data):
    OBSERVATIONS[name] = data
    target = os.environ.get("ACCEPTANCE_OBSERVATIONS")
    if target:
        Path(target).write_text(json.dumps(OBSERVATIONS, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8", newline="\n")


class ProviderFixture:
    """Synthetic prices/usage; never contacts a provider."""
    def __init__(self, pricing_known):
        self._profile = ModelProfile("acceptance-model", provider="acceptance-fixture",
            pricing_known=pricing_known, price_per_1k_input=1., price_per_1k_output=2.)
        self.last_usage = None

    def profile(self): return self._profile

    def estimate_cost(self, *args): return .000001 if self._profile.pricing_known else 0.

    def _usage(self): self.last_usage = {"input_tokens": 100, "output_tokens": 50}

    def generate(self, messages):
        self._usage()
        if "意图分类器" in messages[0].content:
            return json.dumps({"intent": "general", "domain": "general", "confidence": 1})
        return json.dumps({"steps": [{"step_id": "s1", "description": "answer the goal", "deps": []}]})

    def suggest_action(self, messages, tools):
        self._usage()
        return Action(kind="finish", answer="accepted fixture answer")


def run_task(tmp_path, pricing_known):
    app = App(data_dir=str(tmp_path))
    ledger = UsageLedger()
    model = MeteredModel(ProviderFixture(pricing_known), app.bus, ledger=ledger)
    app.model_registry.register(model.profile())
    app.classifier.model = model
    app.router.bind_route("general", "acceptance-model")
    app.router.setup(lambda identity: model if identity == "acceptance-model" else None)
    task = app.controller.submit_task("acceptance goal", wait=True,
        acceptance=[{"type": "result_nonempty"}],
        budget_overrides={"max_cost": .3, "max_cost_per_model_call": 1., "max_model_calls": 10})
    durable = app.db.get_execution_budget(task.task_id)["usage"]
    events = [e.payload for e in app.bus.query(task_id=task.task_id, limit=100)
              if e.event_type == "model_call"]
    result = dict(status=task.task_status.value, provider_calls=len(events),
        provider_recorded_cost=sum(e["cost"] or 0 for e in events),
        provider_unknown_calls=sum(e["cost"] is None for e in events),
        recorded_budget=durable, configured_task_max_cost=.3)
    app.db.close()
    return result


def test_full_task_cost_includes_classifier_and_planner(tmp_path):
    data = run_task(tmp_path, True)
    observe("actual_cost_task", data)
    assert data["provider_calls"] == 3, data
    assert data["recorded_budget"]["cost"] == pytest.approx(data["provider_recorded_cost"]), data


def test_unknown_calls_include_classifier_and_planner(tmp_path):
    data = run_task(tmp_path, False)
    observe("unknown_cost_task", data)
    assert data["provider_unknown_calls"] == 3, data
    assert data["recorded_budget"]["unknown_model_costs"] == 3, data


def test_estimated_usage_is_not_reported_as_actual_cost():
    ledger = UsageLedger()
    ledger.record(ModelUsage(model_id="priced", cost=.5, cost_source=COST_ESTIMATE,
                             token_source=TOKEN_ESTIMATE))
    data = ledger.totals()
    observe("estimated_vs_actual", data)
    assert data["actual_cost"] == 0, data


def test_gc_never_reassigns_an_existing_asset_version():
    conn = sqlite3.connect(":memory:")
    repo = AssetRepository(conn)
    kwargs = dict(asset_id="acceptance:immutable", version="1", asset_type="skill",
                  namespace="domain_private", source_refs=["acceptance:fixture"])
    first = repo.register(content={"revision": "original"}, **kwargs)
    repo.transition(first.asset_id, first.version, "retired")
    repo.collect(first.asset_id, first.version)
    try:
        second = repo.register(content={"revision": "replacement"}, **kwargs)
    except AssetConflict:
        conn.close()
        return
    data = dict(same_ref=str(first.ref) == str(second.ref), original_digest=first.content_digest,
                replacement_digest=second.content_digest,
                event_actions=[e["action"] for e in repo.events(first.asset_id, first.version)])
    observe("identity_after_gc", data)
    conn.close()
    pytest.fail("GC allowed immutable identity reuse: " + json.dumps(data))


def test_prepared_candidate_metadata_and_evidence_are_valid():
    result = subprocess.run([sys.executable, "tools/check_collaboration_plan.py", "--verify-prepared"],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
