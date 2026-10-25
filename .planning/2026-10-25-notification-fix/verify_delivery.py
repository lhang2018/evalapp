"""Verify committed source identity, regression evidence and unchanged acceptance."""
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
prefix = ".planning/2026-10-25-notification-fix/"
os.environ.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
sys.stdout.reconfigure(encoding="utf-8")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=root)


commit = git("rev-parse", sys.argv[1] if len(sys.argv) > 1 else "HEAD").decode().strip()


def blob(path, ref=commit):
    return git("show", ref + ":" + path)


record = json.loads(blob(prefix + "committed-evidence.json"))
source = record["source_commit"]
assert record["source_tree"] == git("rev-parse", source + "^{tree}").decode().strip()
assert not git("diff", source, commit, "--", "evoagent", "tests", "android-app").strip()
assert not git("diff", "f550d90", source, "--", "evoagent", "tests").strip()
hashes = 0
for result in record["results"]:
    assert result["exit_code"] == 0
    assert hashlib.sha256(blob(result["log"])).hexdigest() == result["sha256"]
    hashes += 1
assert b"pass 252" in blob(prefix + "client.txt")
assert b"BUILD SUCCESSFUL" in blob(prefix + "android-build.txt")
before = json.loads(blob(prefix + "before-evidence.json"))
assert before["probe_source_commit"] == source and before["passed"] == 1 and before["failed"] == 15
assert before["exit_code"] == 1
assert hashlib.sha256(blob(before["log"])).hexdigest() == before["sha256"]
hashes += 1
for probe in before["probes"]:
    assert hashlib.sha256(blob(probe["path"])).hexdigest() == probe["sha256"]
    hashes += 1
assert not git("diff", before["before_commit"], commit, "--", "android-app/test/device/notifications.test.ts",
    "android-app/test/offline/background.test.ts").strip()
old = json.loads(blob("docs/协作开发/workboard.json", before["before_commit"]))
new = json.loads(blob("docs/协作开发/workboard.json"))
assert new["dispatches"] == old["dispatches"]
assert new["baseline"]["prepared_candidate"] == old["baseline"]["prepared_candidate"]
assert new["baseline"]["status"] == "candidate" and new["baseline"]["published_commit"] is None
assert new["post_candidate_changes"][:-1] == old["post_candidate_changes"]
entry = new["post_candidate_changes"][-1]
assert len(entry["files"]) == 5 and entry["source_commit"] == source
assert entry["status"] == "fixed_pending_device_validation"
assert entry["device_verified"] is False and entry["independent_review"] is False
assert new["baseline"]["latest_incremental_verification"]["client_passed"] == 252
assert record["device_verified"] is False and record["apk"]["installed"] is False
apk = Path(record["apk"]["path"])
if apk.is_file():
    assert hashlib.sha256(apk.read_bytes()).hexdigest() == record["apk"]["sha256"]

snapshot = root / "outputs" / ("notification-fix-delivery-" + commit[:7])
snapshot.mkdir(exist_ok=False)
with zipfile.ZipFile(io.BytesIO(git("archive", "--format=zip", commit))) as archive:
    archive.extractall(snapshot)


def run(args):
    result = subprocess.run([sys.executable, *args], cwd=snapshot, capture_output=True, text=True, encoding="utf-8")
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
print(json.dumps(dict(metadata_commit=commit, source_commit=source, git_blob_hashes=hashes,
    client_passed=252, typecheck="passed", android_build="passed", device_verified=False,
    baseline_published=False, checks=checks), indent=2))
