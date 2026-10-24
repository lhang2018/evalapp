"""Pin the related runtime suites to the final archive, including API compatibility."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
evidence = json.loads((folder / "committed-evidence.json").read_text(encoding="utf-8"))
command = [sys.executable, "-m", "pytest", *[
    "tests/" + name for name in (
        "test_plan_runtime_review.py", "test_plan_revision.py", "test_planning.py", "test_plan_trace.py",
        "test_process_recovery.py", "test_run_continuation.py", "test_execution_boundaries.py",
        "test_peer_plan_reconcile_review.py", "test_auth_rejection_transport.py", "test_goal_opportunities.py")],
    "-q", "--tb=short", "-W", "error::pytest.PytestUnhandledThreadExceptionWarning"]
log = folder / "related-final.txt"
with log.open("w", encoding="utf-8") as output:
    result = subprocess.run(command, cwd=evidence["snapshot"], stdout=output, stderr=subprocess.STDOUT)
log.write_text(log.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
assert result.returncode == 0 and "207 passed" in log.read_text(encoding="utf-8")
record = dict(source_commit=evidence["source_commit"], source_tree=evidence["source_tree"],
    scope="Named test suites rerun in the same final committed Git archive after the lazy getter compatibility patch",
    results=[dict(name="runtime-related", exit_code=0, command=command, result="207 passed", log=log.relative_to(root).as_posix(),
                  sha256=hashlib.sha256(log.read_bytes()).hexdigest())])
(folder / "related-evidence.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
print(log.read_text(encoding="utf-8")[-400:])
