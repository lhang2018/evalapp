"""Read-only audit probes: only temporary databases/files and inert tools."""
import json
import tempfile
from pathlib import Path

from evoagent.core.budget import BudgetController, BudgetLimits
from evoagent.core.checkpoint import CheckpointManager
from evoagent.core.permissions import PermissionController
from evoagent.core.persistence import Database
from evoagent.core.side_effects import SideEffectLedger
from evoagent.models.base import Action, ModelProfile
from evoagent.react.engine import ReactEngine
from evoagent.release.manager import ReleaseManager
from evoagent.tools.contract import ToolContract, ToolResult
from evoagent.tools.registry import ToolRegistry
from evoagent.workflows.engine import WorkflowEngine
from evoagent.workflows.models import WorkflowDef, WorkflowNode


class FixedModel:
    def __init__(self, first):
        self.first = first
        self.calls = 0

    def profile(self):
        return ModelProfile(model_id="audit-fixed")

    def suggest_action(self, messages, tools):
        self.calls += 1
        return self.first if self.calls == 1 else Action(kind="finish", answer="done")

    def estimate_cost(self, input_chars, output_chars):
        return 0.0


def main():
    result = {}
    with tempfile.TemporaryDirectory(prefix="evoagent-alignment-") as tmp:
        db = Database(str(Path(tmp) / "audit.db"))
        effects = SideEffectLedger(db)
        permissions = PermissionController(high_risk_mode="deny")
        result["workflow_confirmation_decision_in_deny_mode"] = permissions.check_op(
            "workflow_node_confirm", {"node": "sensitive"}).value

        registry = ToolRegistry(PermissionController(high_risk_mode="allow"), db)
        effects = registry.effects
        called = []

        def inert(args):
            called.append(True)
            return ToolResult(ok=True, data="inert", executed=True)

        registry.register(ToolContract(tool_id="outside_expert_allowlist"), inert)
        react = ReactEngine(FixedModel(Action(kind="tool_call", tool="outside_expert_allowlist")), registry)
        react.run([], BudgetController(BudgetLimits()), allowed_tools=[], run_id="audit-whitelist")
        result["tool_outside_empty_allowlist_executed"] = bool(called)

        def timeout(args):
            raise TimeoutError("synthetic unknown remote outcome; no external call")

        registry.register(ToolContract(tool_id="audit_external_timeout", side_effect_level="external", retry_policy=0), timeout)
        tool_result = registry.execute("audit_external_timeout", {}, task_id="audit-timeout")
        effect_id = effects.record_from_tool_result("audit-timeout", "audit_external_timeout", tool_result)
        result["timeout_result"] = {"uncertain": tool_result.side_effect_uncertain,
                                      "evidence": tool_result.evidence,
                                      "ledger_id": effect_id,
                                      "pending": len(effects.pending("audit-timeout"))}

        calls = []
        def unknown(args):
            calls.append(True)
            return ToolResult(ok=False, error="unknown remote outcome", error_class="timeout", side_effect_uncertain=True)

        registry.register(ToolContract(tool_id="audit_external_unknown", side_effect_level="external", retry_policy=0), unknown)
        engine = WorkflowEngine(registry, CheckpointManager(db))
        workflow = WorkflowDef(workflow_id="audit-retry", nodes=[WorkflowNode(node_id="n", tool="audit_external_unknown", max_retries=1)])
        outcome = engine.run(workflow, {})
        result["workflow_unknown_side_effect"] = {"attempts": len(calls), "status": outcome.status,
                                                   "ledger_rows": len(effects.history("audit-retry"))}

        releases = ReleaseManager(signing_secret="audit-only-key", artifact_dir=str(Path(tmp) / "artifacts"))
        manifest = releases.build_manifest(version="audit-original")
        file = Path(tmp) / "artifacts" / f"{manifest.release_id}.json"
        payload = json.loads(file.read_text(encoding="utf-8"))
        payload["version"] = "audit-tampered"
        payload["min_app_version"] = "999.0.0"
        file.write_text(json.dumps(payload), encoding="utf-8")
        result["modified_artifact_accepted"] = releases.verify(manifest.release_id)["ok"]
        file.unlink()
        result["missing_artifact_accepted"] = releases.verify(manifest.release_id)["ok"]
        db.close()
    assert result["workflow_confirmation_decision_in_deny_mode"] == "deny"
    assert not result["tool_outside_empty_allowlist_executed"]
    assert result["timeout_result"]["uncertain"]
    assert result["timeout_result"]["ledger_id"]
    assert result["timeout_result"]["pending"] == 1
    assert result["workflow_unknown_side_effect"]["attempts"] == 1
    assert result["workflow_unknown_side_effect"]["ledger_rows"] == 1
    assert not result["modified_artifact_accepted"]
    assert not result["missing_artifact_accepted"]
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
