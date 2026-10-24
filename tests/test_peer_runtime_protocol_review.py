import pytest
from evoagent.app import App
from evoagent.evolution.sandbox import SandboxRunner
from evoagent.runtime import ExistingRuntimeAdapter, MockRuntime, RuntimeCapability, CapabilityNotSupportedError
from evoagent.runtime.protocol import CapabilityDeclaration
from evoagent.runtime.matrix import assert_matches_declarations


@pytest.mark.parametrize("value", ["false", 1, None])
def test_supported_is_a_real_boolean(value):
    with pytest.raises((TypeError, ValueError)):
        CapabilityDeclaration(RuntimeCapability.CANCEL, value)


def test_mock_never_advertises_a_real_sandbox_even_with_legacy_flag():
    mock = MockRuntime(sandbox_capable=True)
    with pytest.raises(CapabilityNotSupportedError):
        mock.run_sandboxed("raise AssertionError('must not run')")


def test_incomplete_backing_component_is_explicitly_unsupported():
    adapter = ExistingRuntimeAdapter(registry=object(), bus=object(), controller=object(), runs=object(), sandbox=object())
    assert not any(d.supported for d in adapter.declarations())
    with pytest.raises(CapabilityNotSupportedError):
        adapter.invoke_tool("t", {})


def test_duplicate_declarations_cannot_disappear_in_a_matrix_dict():
    class Duplicate(MockRuntime):
        def declarations(self):
            return super().declarations() + [CapabilityDeclaration(RuntimeCapability.CANCEL, False)]
    with pytest.raises((ValueError, AssertionError)):
        assert_matches_declarations(Duplicate(), "mock")
    with pytest.raises((ValueError, AssertionError)):
        Duplicate().cancel_run("t")


def test_mock_event_query_returns_same_prefix_and_zero_limit_as_real_bus(tmp_path):
    app = App(data_dir=str(tmp_path))
    try:
        real = ExistingRuntimeAdapter(bus=app.bus)
        mock = MockRuntime()
        for runtime in (real, mock):
            for n in range(3):
                runtime.emit_event(event_type="review", task_id="t", run_id="r", payload={"n": n})
            assert runtime.query_run_events("r", limit=0) == []
            first = runtime.query_run_events("r", limit=1)[0]
            assert (first["payload"] if isinstance(first, dict) else first.payload)["n"] == 0
    finally:
        app.db.close()


def test_adapter_keeps_actual_tool_policy_and_sandbox_restrictions(tmp_path):
    app = App(data_dir=str(tmp_path))
    try:
        adapter = ExistingRuntimeAdapter(registry=app.tools, sandbox=SandboxRunner())
        result = adapter.invoke_tool("current_time", {}, allowed_tools=[])
        assert not result.executed and result.error_class == "auth_or_permission"
        assert adapter.run_sandboxed("print('probe')", timeout=10).ok
        violation = adapter.run_sandboxed("import socket; socket.socket().connect(('127.0.0.1', 9))", timeout=10)
        assert not violation.ok and violation.violation
    finally:
        app.db.close()
