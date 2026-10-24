"""Verify immutable acceptance evidence and recheck metadata from a Git archive."""
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

root = Path(__file__).resolve().parents[2]
prefix = ".planning/2026-10-24-all-pending-review/"
os.environ.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
sys.stdout.reconfigure(encoding="utf-8")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=root)


commit = git("rev-parse", sys.argv[1] if len(sys.argv) > 1 else "HEAD").decode().strip()


def blob(path, ref=commit):
    return git("show", ref + ":" + path.replace("\\", "/"))


def record(name):
    return json.loads(blob(prefix + name))


hashes = 0
fixed = record("committed-evidence.json")
before = record("before-evidence.json")
for manifest, expected_exit in ((fixed, 0), (before, 1)):
    for result in manifest["results"]:
        assert result["exit_code"] == expected_exit
        assert hashlib.sha256(blob(result["log"])).hexdigest() == result["sha256"], result["log"]
        hashes += 1
assert git("rev-parse", fixed["source_commit"] + "^{tree}").decode().strip() == fixed["source_tree"]
assert not git("diff", fixed["source_commit"], commit, "--", "evoagent", "tests", "android-app").strip()
for probe in before["probes"]:
    assert hashlib.sha256(blob(probe["path"])).hexdigest() == probe["sha256"]
    hashes += 1
for path in ("tests/test_runtime_protocol.py", "android-app/test/device/taskProtocol.test.ts",
             "android-app/test/update/bundleRuntime.test.ts"):
    assert not git("diff", before["source_commit"], commit, "--", path).strip(), path

old = json.loads(blob("docs/协作开发/workboard.json", fixed["source_commit"]))
new = json.loads(blob("docs/协作开发/workboard.json"))
accepted = {"ALN-04-UI", "ALN-07-B", "ALN-11-W", "ALN-12-A", "ALN-20-A"}
assert {c["id"] for c in old["dispatches"] if c["status"] == "review"} == accepted
assert Counter(c["status"] for c in new["dispatches"]) == {"accepted": 40, "blocked": 1}
for original in old["dispatches"]:
    current = next(c for c in new["dispatches"] if c["id"] == original["id"])
    assert current["status"] == ("accepted" if original["id"] in accepted else original["status"])
    for field in ("third_party_review", "third_party_review_partial"):
        if field in original:
            assert current[field] == original[field]
    if original["id"] in accepted:
        assert current["owner"] == original["owner"]
        assert current["reviewer"] == "codex-runtime"
        assert current["acceptance_review"]["source_commit"] == fixed["source_commit"]
        assert current["acceptance_review"]["independent"] is False
        assert current["acceptance_review"]["mode"] == "user_authorized_revalidation"
        assert current["review_authorization"]["source"] == "user_request"
        history = current["review_history"][-1]
        for field in ("reviewer", "review_result", "review_note", "review_report", "fix_status"):
            if field in original:
                assert history[field] == original[field]
assert new["baseline"]["status"] == "candidate" and new["baseline"]["published_commit"] is None
candidate_before = json.loads(blob("docs/协作开发/workboard.json", "eaa1342"))["baseline"]["prepared_candidate"]
candidate = new["baseline"]["prepared_candidate"]
assert candidate["commit"] == git("rev-parse", candidate_before["commit"]).decode().strip()
assert candidate["superseded_by_fix_commit"] == fixed["source_commit"]
for key in ("source_tree", "python_passed", "python_skipped", "client_passed", "typecheck", "remaining_review"):
    assert candidate[key] == candidate_before[key]
for current, original in zip(candidate["evidence"], candidate_before["evidence"]):
    assert current["path"] == original["path"]
    original_blob = blob(current["path"], "eaa1342")
    assert blob(current["path"]) == original_blob
    assert hashlib.sha256(original_blob).hexdigest() == current["sha256"]
    assert current["sha256_basis"] == "lf_normalized_text"
    if current["sha256"] != original["sha256"]:
        assert current["original_working_copy_sha256"] == original["sha256"]
        assert hashlib.sha256(original_blob.replace(b"\n", b"\r\n")).hexdigest() == original["sha256"]
    hashes += 1

audit = record("device-evidence-audit.json")
assert audit["collector"] == "zcode-main" and audit["auditor"] == "codex-runtime"
for evidence in audit["inputs"]:
    if evidence["git_ref"]:
        assert hashlib.sha256(blob(evidence["path"], evidence["git_ref"])).hexdigest() == evidence["sha256"]
        hashes += 1
assert set(audit["task_rows"].values()) == {"CANCELLED", "SUCCEEDED", "SUSPENDED"}
buttons = [n for n in audit["approval_nodes"]["offline"] if n["widget"] == "android.widget.Button"]
assert len(buttons) == 2 and all(n["enabled"] == "false" for n in buttons)
assert any(r["raw_status"] == "pending" and r["effective_status"] == "expired" for r in audit["approval_rows"])
workflow = audit["workflow"]
assert workflow["phase"] == "rolled_back" and workflow["recovery"]["outcome"] == "succeeded"
assert workflow["active"]["digest"] == workflow["recovery"]["recovered"]["digest"]
assert workflow["active"]["digest"] != workflow["recovery"]["failed"]["digest"]
assert any(r["execution_id"] == workflow["recovery"]["execution_id"] and r["outcome"] == "succeeded"
           for r in workflow["records"])

snapshot = root / "outputs" / ("all-pending-metadata-" + commit[:7])
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

spec = importlib.util.spec_from_file_location("acceptance_board_checker", snapshot / "tools/check_collaboration_plan.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)
assert not checker.validate(new)
negative_checks = []
for case in ("missing_authorization", "independent_true", "non_boolean_permission", "missing_report"):
    board = copy.deepcopy(new)
    card = next(c for c in board["dispatches"] if c["id"] == "ALN-04-UI")
    if case == "missing_authorization":
        card.pop("review_authorization")
    elif case == "independent_true":
        card["review_authorization"]["independent"] = True
    elif case == "non_boolean_permission":
        card["review_authorization"]["allow_same_author"] = "true"
    else:
        card["acceptance_review"].pop("report")
    assert "task author cannot be their own reviewer: ALN-04-UI" in checker.validate(board), case
    negative_checks.append(case)
evidence = candidate["evidence"][0]
log = snapshot / evidence["path"]
saved = log.read_bytes()
try:
    canonical = saved.replace(b"\r\n", b"\n")
    for data in (canonical, canonical.replace(b"\n", b"\r\n")):
        log.write_bytes(data)
        assert not checker.verify_prepared(new)
    log.write_bytes(canonical + b"changed test result\n")
    assert "prepared evidence changed: " + evidence["path"] in checker.verify_prepared(new)
finally:
    log.write_bytes(saved)
print(json.dumps(dict(metadata_commit=commit, source_commit=fixed["source_commit"],
    verified_git_blob_hashes=hashes, checks=checks, rejected_same_author_cases=negative_checks,
    prepared_evidence_checks="LF/CRLF accepted; altered content rejected"), indent=2))
