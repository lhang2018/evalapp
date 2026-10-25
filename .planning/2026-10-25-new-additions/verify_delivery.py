"""Check immutable incremental evidence and archived documentation/coverage checks."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

root = Path(__file__).resolve().parents[2]
prefix = ".planning/2026-10-25-new-additions/"
os.environ.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
sys.stdout.reconfigure(encoding="utf-8")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=root)


commit = git("rev-parse", sys.argv[1] if len(sys.argv) > 1 else "HEAD").decode().strip()


def blob(path, ref=commit):
    return git("show", ref + ":" + path)


verification = json.loads(blob(prefix + "verification.json"))
source = verification["source_commit"]
assert not git("diff", source, commit, "--", "evoagent", "android-app", "tests").strip()
assert not git("diff", "f550d90", source, "--", "evoagent", "tests").strip()
for result in verification["results"]:
    assert result["exit_code"] == 0
    assert hashlib.sha256(blob(result["log"])).hexdigest() == result["sha256"]
assert b"pass 236" in blob(prefix + "client.txt")
old = json.loads(blob("docs/协作开发/workboard.json", source))
new = json.loads(blob("docs/协作开发/workboard.json"))
assert new["dispatches"] == old["dispatches"]
assert new["baseline"]["prepared_candidate"] == old["baseline"]["prepared_candidate"]
assert new["baseline"]["status"] == "candidate" and new["baseline"]["published_commit"] is None
assert len(new["post_candidate_changes"]) == 1
entry = new["post_candidate_changes"][0]
assert entry["status"] == "committed_pending_acceptance"
assert len(entry["files"]) == 6 and entry["candidate_included"] is False and entry["device_verified"] is False
assert json.loads(blob(prefix + "coverage-smoke.json"))["passed"] == 8
snapshot = root / "outputs" / ("new-additions-delivery-" + commit[:7])
snapshot.mkdir(exist_ok=False)
with zipfile.ZipFile(io.BytesIO(git("archive", "--format=zip", commit))) as archive:
    archive.extractall(snapshot)


def run(args):
    command = [sys.executable, *args]
    result = subprocess.run(command, cwd=snapshot, capture_output=True, encoding="utf-8", text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return dict(command=args, exit_code=result.returncode)


with ThreadPoolExecutor(3) as pool:
    checks = list(pool.map(run, [
        ["tools/check_collaboration_plan.py", "--verify-prepared"],
        ["tools/check_source_coverage.py", "--root", str(snapshot), "--head", commit],
        ["tools/verify_claims.py", "--check"],
        ["tools/scan_all_ledgers.py"],
        ["tools/check_gap_closure.py"],
        [".planning/2026-10-22-design-alignment/verify_alignment_plan.py"],
    ]))
print(json.dumps(dict(delivery_commit=commit, source_commit=source, backend_evidence_reused=True,
    client_passed=236, typecheck="passed", feature_accepted=False, baseline_published=False, checks=checks), indent=2))
