"""Verify pinned runtime review evidence and documentation from Git blobs."""
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
os.environ.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
sys.stdout.reconfigure(encoding="utf-8")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=root)


commit = git("rev-parse", sys.argv[1] if len(sys.argv) > 1 else "HEAD").decode().strip()
prefix = ".planning/2026-10-24-plan-runtime-review/"
count = 0
for name in ("committed-evidence.json", "related-evidence.json", "first-attempt-e54ad2a.json", "second-attempt-eea3a00.json"):
    evidence = json.loads(git("show", commit + ":" + prefix + name))
    for entry in evidence["results"]:
        path = entry["log"].replace("\\", "/")
        assert hashlib.sha256(git("show", commit + ":" + path)).hexdigest() == entry["sha256"], path
        count += 1
    assert git("rev-parse", evidence["source_commit"] + "^{tree}").decode().strip() == evidence["source_tree"]
    if name in ("committed-evidence.json", "related-evidence.json"):
        assert not git("diff", evidence["source_commit"], commit, "--", "evoagent", "tests", "android-app").strip()
        assert all(r["exit_code"] == 0 for r in evidence["results"])
    else:
        assert next(r for r in evidence["results"] if r["name"] == "backend-committed")["exit_code"] == 1
before = json.loads(git("show", commit + ":" + prefix + "before-evidence.json"))
assert before["exit_code"] == 1
assert hashlib.sha256(git("show", commit + ":" + before["log"].replace("\\", "/"))).hexdigest() == before["sha256"]
count += 1
for probe in before["injected_probes"]:
    assert hashlib.sha256(git("show", commit + ":" + probe["path"])).hexdigest() == probe["sha256"]
    count += 1
assert not git("diff", before["source_commit"], commit, "--", "tests/test_plan_revision.py", "tests/test_run_continuation.py").strip()
old_board = json.loads(git("show", before["source_commit"] + ":docs/协作开发/workboard.json"))
new_board = json.loads(git("show", commit + ":docs/协作开发/workboard.json"))
old_states = {c["id"]: c["status"] for c in old_board["dispatches"]}
new_states = {c["id"]: c["status"] for c in new_board["dispatches"]}
assert {k: (old_states[k], new_states[k]) for k in old_states if old_states[k] != new_states[k]} == {"ALN-07-E": ("review", "accepted")}
peer_board = json.loads(git("show", "e215d23582698d70096f7fc8e3570d71b091fabb:docs/协作开发/workboard.json"))
peer_card = next(c for c in peer_board["dispatches"] if c["id"] == "ALN-07-E")
accepted_card = next(c for c in new_board["dispatches"] if c["id"] == "ALN-07-E")
assert peer_card["third_party_review"] == accepted_card["third_party_review"]
assert accepted_card["fix_status"] == "fix_verified_by_third_party"
assert old_board["baseline"]["prepared_candidate"] == new_board["baseline"]["prepared_candidate"]
assert new_board["baseline"]["published_commit"] is None and new_board["baseline"]["status"] == "candidate"
old_card = next(c for c in old_board["dispatches"] if c["id"] == "ALN-07-P")
new_card = next(c for c in new_board["dispatches"] if c["id"] == "ALN-07-P")
assert old_card["third_party_review_partial"] == new_card["third_party_review_partial"]
snapshot = root / "outputs" / ("plan-runtime-review-metadata-" + commit[:7])
snapshot.mkdir(exist_ok=False)
with zipfile.ZipFile(io.BytesIO(git("archive", "--format=zip", commit))) as z:
    z.extractall(snapshot)


def check(command):
    result = subprocess.run(command, cwd=snapshot, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    return dict(command=command, exit_code=result.returncode)


with ThreadPoolExecutor(3) as pool:
    checks = list(pool.map(check, [
        [sys.executable, "tools/check_collaboration_plan.py", "--verify-prepared"],
        [sys.executable, "tools/verify_claims.py", "--check"],
        [sys.executable, "tools/scan_all_ledgers.py"],
        [sys.executable, "tools/check_gap_closure.py"],
        [sys.executable, ".planning/2026-10-22-design-alignment/verify_alignment_plan.py"],
    ]))
print(json.dumps(dict(metadata_commit=commit, verified_git_blob_hashes=count, checks=checks), indent=2))
