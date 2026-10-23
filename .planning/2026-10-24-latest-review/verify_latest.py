"""Review latest evidence against immutable Git archives, without dirty files."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

root = Path(__file__).resolve().parents[2]
evidence_dir = Path(__file__).resolve().parent
sys.stdout.reconfigure(encoding="utf-8")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=root)


current = git("rev-parse", "6a4c0b5").decode().strip()
before = git("rev-parse", "dfcfcee").decode().strip()
fixed = git("rev-parse", "f633130").decode().strip()
assert not git("diff", fixed, current, "--", "evoagent", "tests", "android-app").strip()
original_tests = ["tests/test_plan_trace.py", "tests/test_recovery_policy.py", "tests/test_export_deletion_exec.py"]
assert not git("diff", before, current, "--", *original_tests).strip()
snapshots = {}
for name, commit in (("before", before), ("latest", current)):
    path = Path(tempfile.mkdtemp(prefix="latest-review-" + commit[:7] + "-", dir=root / "outputs"))
    with zipfile.ZipFile(io.BytesIO(git("archive", "--format=zip", commit))) as archive:
        archive.extractall(path)
    probe = ".planning/2026-10-24-fix3-review/probe_fix3.py"
    (path / probe).parent.mkdir(parents=True, exist_ok=True)
    (path / probe).write_bytes(git("show", current + ":" + probe))
    snapshots[name] = path
    (path / "outputs").mkdir(exist_ok=True)


def run(job):
    name, command, cwd = job
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(cwd)}
    log = evidence_dir / (name + ".txt")
    with log.open("w", encoding="utf-8") as output:
        result = subprocess.run(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT)
    log.write_text(log.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    return dict(name=name, command=command, exit_code=result.returncode,
                log=str(log.relative_to(root)), sha256=hashlib.sha256(log.read_bytes()).hexdigest())


jobs = [("probe-before", [sys.executable, ".planning/2026-10-24-fix3-review/probe_fix3.py"], snapshots["before"]),
        ("probe-latest", [sys.executable, ".planning/2026-10-24-fix3-review/probe_fix3.py"], snapshots["latest"]),
        ("related-latest", [sys.executable, "-m", "pytest", "-q", "--tb=short",
            "--basetemp", str(snapshots["latest"] / "outputs/pytest-tmp"),
            *original_tests, "tests/test_peer_trace_export_review.py", "tests/test_observability_evidence.py",
            "tests/test_plan_revision.py", "tests/test_planning.py"], snapshots["latest"])]
with ThreadPoolExecutor(3) as pool:
    results = list(pool.map(run, jobs))
record = dict(reviewed_commit=current, before_commit=before, identical_code_to=fixed,
              scope="latest changes only add evidence/workboard/memory; code and original tests unchanged",
              probe_sha256=hashlib.sha256(git("show", current + ":.planning/2026-10-24-fix3-review/probe_fix3.py")).hexdigest(),
              results=results)
(evidence_dir / "evidence.json").write_text(json.dumps(record, indent=2), encoding="utf-8", newline="\n")
assert results[0]["exit_code"] == 1 and all(r["exit_code"] == 0 for r in results[1:])
for result in results:
    print(result["name"], result["exit_code"])
    print((root / result["log"]).read_text(encoding="utf-8")[-700:])
