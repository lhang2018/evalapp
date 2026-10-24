"""Build Android only from the exact exported candidate and installed lockfile dependencies."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
evidence = Path(__file__).resolve().parent
source = json.loads((evidence / "candidate-source.json").read_text(encoding="utf-8"))
snapshot = Path(source["snapshot"])
os.environ.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
results = []
for name, cmd, cwd in [
    ("typecheck", ["node", "node_modules/typescript/bin/tsc", "--noEmit"], snapshot / "android-app"),
    ("android-build", ["cmd", "/c", "gradlew.bat", ":app:assembleDebug", "-PreactNativeArchitectures=arm64-v8a",
        "-PreactNativeDevServerPort=8084", "--console=plain"], snapshot / "android-app/android"),
]:
    log = evidence / (name + ".txt")
    with log.open("w", encoding="utf-8") as out:
        run = subprocess.run(cmd, cwd=cwd, stdout=out, stderr=subprocess.STDOUT)
    log.write_text(log.read_text(encoding="utf-8", errors="replace"), encoding="utf-8", newline="\n")
    results.append(dict(name=name, command=cmd, exit_code=run.returncode, log=log.relative_to(root).as_posix(),
        sha256=hashlib.sha256(log.read_bytes()).hexdigest(), sha256_basis="lf_normalized_text"))
    print(name, run.returncode, flush=True)
    if run.returncode:
        break
record = dict(source_commit=source["commit"], source_tree=source["tree"], results=results,
    dependency_install="npm ci --no-audit --no-fund; lockfile exact; no shared node_modules junction",
    typescript=json.loads((snapshot / "android-app/node_modules/typescript/package.json").read_text())["version"])
apk = snapshot / "android-app/android/app/build/outputs/apk/debug/app-debug.apk"
if results[-1]["name"] == "android-build" and results[-1]["exit_code"] == 0:
    record["apk"] = dict(path=str(apk), sha256=hashlib.sha256(apk.read_bytes()).hexdigest(), size=apk.stat().st_size)
(evidence / "build-evidence.json").write_text(json.dumps(record, indent=2), encoding="utf-8", newline="\n")
raise SystemExit(0 if len(results) == 2 and all(r["exit_code"] == 0 for r in results) else 1)
