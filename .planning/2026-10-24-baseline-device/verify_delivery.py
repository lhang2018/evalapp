"""Verify the committed handoff and all evidence from canonical Git blobs."""
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

root = Path(__file__).resolve().parents[2]
prefix = ".planning/2026-10-24-baseline-device/"
os.environ.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
sys.stdout.reconfigure(encoding="utf-8")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=root)


commit = git("rev-parse", sys.argv[1] if len(sys.argv) > 1 else "HEAD").decode().strip()


def blob(path, ref=commit):
    return git("show", ref + ":" + path.replace("\\", "/"))


def record(name):
    return json.loads(blob(prefix + name))


source = record("candidate-source.json")
validation, build, device = (record(name) for name in ("candidate-validation.json", "build-evidence.json", "device-attestation.json"))
assert source["commit"] == git("rev-parse", "f550d90").decode().strip()
assert source["tree"] == git("rev-parse", source["commit"] + "^{tree}").decode().strip()
assert not git("diff", source["commit"], commit, "--", "evoagent", "tests", "android-app", "tools").strip()
hashes = 0
for result in validation["results"] + build["results"]:
    assert result["exit_code"] == 0
    assert hashlib.sha256(blob(result["log"])).hexdigest() == result["sha256"], result["log"]
    hashes += 1
assert b"1661 passed, 1 skipped" in blob(prefix + "backend.txt")
assert b"pass 228" in blob(prefix + "client.txt")
assert b"BUILD SUCCESSFUL" in blob(prefix + "android-build.txt")
assert device["source_commit"] == build["source_commit"] == source["commit"]
assert device["installed_apk_sha256"] == build["apk"]["sha256"]
assert device["tracked_android_files_unchanged"] == 102
for path, digest in device["important_source_sha256"].items():
    assert hashlib.sha256(blob(path, source["commit"])).hexdigest() == digest, path
    hashes += 1
assert device["product_entry_restored"] and device["actual_force_stop"]
before, after = record("device-before-restart.json"), record("device-after-restart.json")
assert before["phase"] == "awaiting-force-stop" and after["phase"] == "done"
assert before["engine"] == after["engine"] == "Hermes"
assert len(before["tests"]) == 7 and len(after["tests"]) == 9 and all(t["ok"] for t in after["tests"])
force = record("force-stop-evidence.json")
assert force["before_pid"] and not force["after_pid"] and force["killed"] is True
tests = {test["name"]: test for test in after["tests"]}
assert tests["concurrent_same_action_dispatches_once"]["detail"]["dispatches"] == 1
assert tests["actual_force_stop_restart_keeps_receipts_and_never_replays_unknown"]["detail"]["dispatches"] == 0
assert tests["hard_tool_timeout_no_retry_or_downstream_after_late_completion"]["detail"]["probe"] == "unknown"
assert tests["unhealthy_bundle_rolls_back_and_executes_old_native_artifact"]["detail"]["recovery"]["outcome"] == "succeeded"
fixtures = record("device-fixtures.json")
for kind in ("good", "bad", "timeout"):
    assert hashlib.sha256(blob(prefix + kind + ".bundle")).hexdigest() == fixtures["targets"][kind]["artifact_sha256"]
    hashes += 1
board = json.loads(blob("docs/协作开发/workboard.json"))
old = json.loads(blob("docs/协作开发/workboard.json", source["commit"]))
assert Counter(c["status"] for c in board["dispatches"]) == {"accepted": 40, "review": 1}
for card in old["dispatches"]:
    current = next(c for c in board["dispatches"] if c["id"] == card["id"])
    if card["id"] != "ALN-00-B":
        assert current == card, card["id"]
    else:
        assert current["status"] == "review" and current["reviewer"] is None
assert board["baseline"]["status"] == "candidate" and board["baseline"]["published_commit"] is None
prepared = board["baseline"]["prepared_candidate"]
assert prepared["commit"] == source["commit"] and prepared["source_tree"] == source["tree"]
assert prepared["technical_verification_complete"] and not prepared["publication_ready"]
assert board["baseline"]["prepared_candidate_history"][-1] == old["baseline"]["prepared_candidate"]

snapshot = root / "outputs" / ("baseline-device-delivery-" + commit[:7])
snapshot.mkdir(exist_ok=False)
with zipfile.ZipFile(io.BytesIO(git("archive", "--format=zip", commit))) as archive:
    archive.extractall(snapshot)


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
print(json.dumps(dict(delivery_commit=commit, candidate_commit=source["commit"],
    verified_git_blob_hashes=hashes, device_tests=9, baseline_published=False, checks=checks), indent=2))
