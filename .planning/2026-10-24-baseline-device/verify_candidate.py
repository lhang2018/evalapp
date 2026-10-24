"""Export the exact candidate and run all declared acceptance checks."""
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
evidence = Path(__file__).resolve().parent
os.environ.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
commit = subprocess.check_output(["git", "rev-parse", sys.argv[1]], cwd=root, text=True).strip()
snapshot = root / "outputs" / ("baseline-device-" + commit[:7])
snapshot.mkdir(parents=True, exist_ok=False)
with zipfile.ZipFile(io.BytesIO(subprocess.check_output(
        ["git", "-c", "core.autocrlf=false", "archive", "--format=zip", commit], cwd=root))) as archive:
    archive.extractall(snapshot)
(evidence / "candidate-source.json").write_text(json.dumps(dict(commit=commit, tree=subprocess.check_output(
    ["git", "rev-parse", commit + "^{tree}"], cwd=root, text=True).strip(), snapshot=str(snapshot)), indent=2),
    encoding="utf-8", newline="\n")


def run(job):
    name, command, cwd = job
    log = evidence / (name + ".txt")
    with log.open("w", encoding="utf-8") as out:
        result = subprocess.run(command, cwd=cwd, stdout=out, stderr=subprocess.STDOUT)
    log.write_text(log.read_text(encoding="utf-8", errors="replace"), encoding="utf-8", newline="\n")
    print(name, result.returncode, flush=True)
    return dict(name=name, command=command, exit_code=result.returncode,
                log=log.relative_to(root).as_posix(), sha256=hashlib.sha256(log.read_bytes()).hexdigest(),
                sha256_basis="lf_normalized_text")


jobs = [
    ("backend", [sys.executable, "-m", "pytest", "-q", "--tb=short", "-W",
        "error::pytest.PytestUnhandledThreadExceptionWarning"], snapshot),
    ("client", ["node", "--test", "test/offline/*.test.ts", "test/device/*.test.ts", "test/update/*.test.ts"], snapshot / "android-app"),
    ("claims", [sys.executable, "tools/verify_claims.py", "--check"], snapshot),
    ("ledgers", [sys.executable, "tools/scan_all_ledgers.py"], snapshot),
    ("board", [sys.executable, "tools/check_collaboration_plan.py", "--verify-prepared"], snapshot),
    ("gap", [sys.executable, "tools/check_gap_closure.py"], snapshot),
    ("alignment", [sys.executable, ".planning/2026-10-22-design-alignment/verify_alignment_plan.py"], snapshot),
]
with ThreadPoolExecutor(3) as pool:
    results = list(pool.map(run, jobs))
record = json.loads((evidence / "candidate-source.json").read_text(encoding="utf-8"))
record.update(results=results, dependency_scope="Python installed environment; node tests use built-in runner; separate dependency/typecheck/build evidence follows")
(evidence / "candidate-validation.json").write_text(json.dumps(record, indent=2), encoding="utf-8", newline="\n")
raise SystemExit(0 if all(r["exit_code"] == 0 for r in results) else 1)
