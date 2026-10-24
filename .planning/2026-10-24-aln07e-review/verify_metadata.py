"""Verify pinned reconciliation evidence and documentation from Git blobs."""
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
prefix = ".planning/2026-10-24-aln07e-review/"
count = 0
for name in ("committed-evidence.json", "related-evidence.json"):
    evidence = json.loads(git("show", commit + ":" + prefix + name))
    for entry in evidence["results"]:
        path = entry["log"].replace("\\", "/")
        assert hashlib.sha256(git("show", commit + ":" + path)).hexdigest() == entry["sha256"], path
        count += 1
    assert git("rev-parse", evidence["source_commit"] + "^{tree}").decode().strip() == evidence["source_tree"]
    assert not git("diff", evidence["source_commit"], commit, "--", "evoagent", "tests", "android-app").strip()
before = json.loads(git("show", commit + ":" + prefix + "before-evidence.json"))
assert hashlib.sha256(git("show", commit + ":" + before["log"].replace("\\", "/"))).hexdigest() == before["sha256"]
assert hashlib.sha256(git("show", commit + ":" + before["injected_probe"])).hexdigest() == before["probe_sha256"]
assert not git("diff", before["source_commit"], commit, "--", "tests/test_plan_side_effect_reconcile.py").strip()
count += 2
snapshot = root / "outputs" / ("aln07e-review-metadata-" + commit[:7])
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
