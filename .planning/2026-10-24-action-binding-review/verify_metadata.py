"""Attest committed evidence, source identity and preserved independent decisions."""
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
prefix = ".planning/2026-10-24-action-binding-review/"
os.environ.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
sys.stdout.reconfigure(encoding="utf-8")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=root)


commit = git("rev-parse", sys.argv[1] if len(sys.argv) > 1 else "HEAD").decode().strip()
hashes = 0
for name in ("committed-evidence.json", "related-evidence.json"):
    record = json.loads(git("show", commit + ":" + prefix + name))
    for result in record["results"]:
        path = result["log"].replace("\\", "/")
        assert result["exit_code"] == 0
        assert hashlib.sha256(git("show", commit + ":" + path)).hexdigest() == result["sha256"], path
        hashes += 1
    assert git("rev-parse", record["source_commit"] + "^{tree}").decode().strip() == record["source_tree"]
    assert not git("diff", record["source_commit"], commit, "--", "evoagent", "tests", "android-app").strip()
before = json.loads(git("show", commit + ":" + prefix + "before-evidence.json"))
assert before["exit_code"] == 1
assert hashlib.sha256(git("show", commit + ":" + before["log"].replace("\\", "/"))).hexdigest() == before["sha256"]
assert hashlib.sha256(git("show", commit + ":" + before["injected_probe"])).hexdigest() == before["probe_sha256"]
assert not git("diff", before["source_commit"], commit, "--", "tests/test_action_binding.py").strip()
hashes += 2
source = json.loads(git("show", commit + ":" + prefix + "committed-evidence.json"))["source_commit"]
old = json.loads(git("show", source + ":docs/协作开发/workboard.json"))
new = json.loads(git("show", commit + ":docs/协作开发/workboard.json"))
assert {c["id"]: c["status"] for c in old["dispatches"]} == {c["id"]: c["status"] for c in new["dispatches"]}
for card in old["dispatches"]:
    after = next(c for c in new["dispatches"] if c["id"] == card["id"])
    for field in ("third_party_review", "third_party_review_partial"):
        if field in card:
            assert after[field] == card[field]
assert old["baseline"]["prepared_candidate"] == new["baseline"]["prepared_candidate"]
assert new["baseline"]["status"] == "candidate" and new["baseline"]["published_commit"] is None
snapshot = root / "outputs" / ("action-binding-review-metadata-" + commit[:7])
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
print(json.dumps(dict(metadata_commit=commit, verified_git_blob_hashes=hashes, checks=checks), indent=2))
