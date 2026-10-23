"""Pin a Git object and verify only its committed files in this project."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
root = Path(__file__).resolve().parents[2]
evidence = Path(__file__).resolve().parent
commit = subprocess.check_output(["git", "rev-parse", sys.argv[1]], cwd=root, text=True).strip()
snapshot = root / "outputs" / f"peer-{commit[:7]}"
snapshot.mkdir(parents=True, exist_ok=False)
archive = subprocess.check_output(["git", "archive", "--format=zip", commit], cwd=root)
with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
    zipped.extractall(snapshot)
os.environ.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")

def run(job):
    name, command, cwd = job
    log = evidence / f"{name}.txt"
    with log.open("w", encoding="utf-8") as output:
        result = subprocess.run(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT)
    # Git's text normalization must not change the attested bytes.
    log.write_text(log.read_text(encoding="utf-8", errors="replace"), encoding="utf-8", newline="\n")
    return dict(name=name, command=command, exit_code=result.returncode,
                log=str(log.relative_to(root)), sha256=hashlib.sha256(log.read_bytes()).hexdigest())

jobs = [
    ("backend-committed", [sys.executable, "-m", "pytest", "-q", "--tb=short",
        "-W", "error::pytest.PytestUnhandledThreadExceptionWarning"], snapshot),
    ("client-committed", ["node", "--test", "test/offline/*.test.ts", "test/device/*.test.ts", "test/update/*.test.ts"], snapshot / "android-app"),
    ("claims-committed", [sys.executable, "tools/verify_claims.py", "--check"], snapshot),
    ("ledgers-committed", [sys.executable, "tools/scan_all_ledgers.py"], snapshot),
    ("board-committed", [sys.executable, "tools/check_collaboration_plan.py"], snapshot),
]
reused = []
previous_file = root / ".planning/2026-10-23-aln15/committed-8c479b6.json"
if previous_file.exists():
    previous = json.loads(previous_file.read_text(encoding="utf-8"))
    def client_tree(ref):
        return subprocess.check_output(["git", "rev-parse", ref + ":android-app"], cwd=root).strip()
    if client_tree(commit) == client_tree(previous["source_commit"]):
        for result in previous["results"]:
            if result["name"] in {"client-committed", "typecheck-committed"}:
                assert result["exit_code"] == 0
                assert hashlib.sha256((root / result["log"]).read_bytes()).hexdigest() == result["sha256"]
                reused.append({**result, "reused_from_source_commit": previous["source_commit"],
                               "reuse_reason": "identical android-app Git tree"})
        jobs = [job for job in jobs if job[0] != "client-committed"]
with ThreadPoolExecutor(max_workers=5) as pool:
    results = list(pool.map(run, jobs))
deps = root / "android-app/node_modules"
if deps.exists() and not reused:
    subprocess.run(["powershell", "-NoProfile", "-Command",
        f"New-Item -ItemType Junction -Path '{snapshot / 'android-app/node_modules'}' -Target '{deps}' | Out-Null"], check=True)
    results.append(run(("typecheck-committed", ["node", "node_modules/typescript/bin/tsc", "--noEmit"], snapshot / "android-app")))
results.extend(reused)
record = dict(source_commit=commit, source_tree=subprocess.check_output(
    ["git", "rev-parse", commit + "^{tree}"], cwd=root, text=True).strip(),
    scope="git archive; excludes dirty working-tree files; typecheck reuses installed dependencies",
    snapshot=str(snapshot), results=results)
(evidence / "committed-evidence.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
for item in results:
    print(item["name"], item["exit_code"])
    print((root / item["log"]).read_text(encoding="utf-8")[-700:])
