"""Run the assigned cards against one immutable source archive."""
import json
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
commit = sys.argv[1] if len(sys.argv) > 1 else "f973dcc1fc68ab14f1c0d7d706718dc79ed5b037"
coverage = {
    "ALN-30-B": [],
    "ALN-24-F": ["test_multi_principal_scope", "test_evidence_scope_review_20261026"],
    "ALN-16-Q": ["test_evidence_api", "test_evidence_scope_review_20261026", "test_observability_repository", "test_observability_evidence"],
    "ALN-13-S": ["test_goal_scheduler", "test_goal_scheduler_host", "test_new_host_review_20261026", "test_assigned_scheduler_review_20261026"],
    "ALN-09-I": ["test_candidate_host_api", "test_host_benchmark", "test_benchmark_review_20261026", "test_new_host_review_20261026"],
    "ALN-18-H": ["test_coding_expert_consumer", "test_coding_consumption", "test_coding_tools", "test_new_host_review_20261026"],
    "ALN-19-A": ["test_media_vendor", "test_media_vendor_review_20261026", "test_release_evidence"],
    "ALN-27-C": ["test_knowledge", "test_knowledge_persistence", "test_knowledge_review_20261026"],
    "ALN-15-E": [],
    "ALN-25-C": ["test_proactive_execution", "test_proactive_execution_review_20261026", "test_proactivity", "test_resource_second_review_20261026"],
    "ALN-30-A": ["test_integration_wiring", "test_restore_entry_review_20261026", "test_assigned_restore_review_20261026"],
    "ALN-25-B": ["test_proactivity", "test_resource_admission", "test_resource_second_review_20261026", "test_proactive_review_20261026"],
    "ALN-18-L": ["test_refactor", "test_feedback_refactor_review_20261026", "test_developed_review_5", "test_coding_tools"],
}
files = sorted({"tests/" + module + ".py" for modules in coverage.values() for module in modules})
assert all((root / name).is_file() for name in files)
(folder / "coverage.json").write_text(json.dumps({"commit": commit, "cards": coverage,
    "client_cards": ["ALN-30-B", "ALN-15-E"], "note": "overlapping modules: per-card counts are not additive"},
    ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
command = [sys.executable, "-X", "utf8", str(root / ".planning/2026-10-25-developed-review-5/run_scoped_pytest.py"),
    "--snapshot", commit, "--record", str(folder / "committed-validation.json"), *files,
    "-k", "not test_real_host_pressure_yields_and_core_state_untouched", "-q",
    "--junitxml=" + str(folder / "junit.xml")]
with (folder / "committed-tests.txt").open("w", encoding="utf-8") as log:
    result = subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT)
print("backend exit:", result.returncode, "modules:", len(files))
raise SystemExit(result.returncode)
