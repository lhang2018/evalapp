"""Resource second-review probes; injected samples do not claim host load."""
from types import SimpleNamespace

import pytest

from evoagent.core.resource_admission import ResourceGovernor
from evoagent.goals.proactivity import ProactivityGovernor


@pytest.mark.parametrize("snapshot", [{}, {"cpu": 0.1}, None])
def test_proactive_start_requires_explicit_sample_validity(snapshot):
    class Adapter:
        def admit(self, origin):
            return SimpleNamespace(decision="admit", snapshot=snapshot, reason="probe")

    assert not ProactivityGovernor(resource_governor=Adapter()).may_start().allowed


@pytest.mark.parametrize("invalid", [{}, {"cpu": float("nan")}, {"cpu": False}, "raises"])
def test_invalid_sample_does_not_resume_or_forget_paused_task(invalid):
    sample = {"value": {"cpu": 0.95}}

    def read():
        if sample["value"] == "raises":
            raise RuntimeError("synthetic unavailable sampler")
        return sample["value"]

    governor = ResourceGovernor(read)
    assert governor.should_pause("synthetic-task", "opportunity").decision == "pause"
    sample["value"] = invalid
    assert governor.resume_candidates() == {}
    assert "synthetic-task" in governor.paused_tasks
    sample["value"] = {"cpu": 0.69}
    assert governor.resume_candidates()["synthetic-task"].decision == "resume"


def test_valid_hysteresis_and_legacy_admission_are_preserved():
    state = {"cpu": 0.95}
    governor = ResourceGovernor(lambda: dict(state))
    governor.should_pause("synthetic-task", "opportunity")
    state["cpu"] = 0.8
    assert governor.admit("opportunity").decision == "admit"
    assert governor.resume_candidates() == {}
    state["cpu"] = 0.7
    assert governor.resume_candidates()["synthetic-task"].decision == "resume"
    # Existing foreground admission remains explicitly fail-open, but proactive
    # consumption rejects the unavailable sample.
    unavailable = ResourceGovernor(lambda: {})
    assert unavailable.admit("user").decision == "admit"
    assert not ProactivityGovernor(resource_governor=unavailable).may_start().allowed
