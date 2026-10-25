"""Test and build the exact committed notification fix, excluding working files."""
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
out = Path(__file__).resolve().parent
os.environ.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
sys.stdout.reconfigure(encoding="utf-8")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=root)


source = git("rev-parse", sys.argv[1]).decode().strip()
snapshot = root / "outputs" / ("notification-fix-" + source[:7])
snapshot.mkdir(exist_ok=False)
with zipfile.ZipFile(io.BytesIO(git("-c", "core.autocrlf=false", "archive", "--format=zip", source))) as archive:
    archive.extractall(snapshot)
assert git("rev-parse", source + ":android-app/package-lock.json") == git("rev-parse", "f550d90:android-app/package-lock.json")
assert not git("diff", "f550d90", source, "--", "evoagent", "tests").strip()
deps = root / "outputs/baseline-device-f550d90/android-app/node_modules"
assert deps.is_dir()
subprocess.run(["powershell", "-NoProfile", "-Command", f"New-Item -ItemType Junction -Path '{snapshot / 'android-app/node_modules'}' -Target '{deps}' | Out-Null"], check=True, capture_output=True)


def run(job):
    name, command, cwd = job
    log = out / (name + ".txt")
    with log.open("w", encoding="utf-8") as output:
        result = subprocess.run(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT)
    log.write_text(log.read_text(encoding="utf-8", errors="replace"), encoding="utf-8", newline="\n")
    print(name, result.returncode, flush=True)
    return dict(name=name, command=command, exit_code=result.returncode,
        log=log.relative_to(root).as_posix(), sha256=hashlib.sha256(log.read_bytes()).hexdigest(), sha256_basis="lf_normalized_text")


jobs = [
    ("client", ["node", "--test", "test/offline/*.test.ts", "test/device/*.test.ts", "test/update/*.test.ts"], snapshot / "android-app"),
    ("typecheck", ["node", "node_modules/typescript/bin/tsc", "--noEmit"], snapshot / "android-app"),
    ("android-build", ["cmd", "/c", "gradlew.bat", ":app:assembleDebug", "-PreactNativeArchitectures=arm64-v8a", "--console=plain"], snapshot / "android-app/android"),
]
with ThreadPoolExecutor(3) as pool:
    results = list(pool.map(run, jobs))
record = dict(source_commit=source, source_tree=git("rev-parse", source + "^{tree}").decode().strip(),
    results=results, backend_evidence_reused_from="f550d9063f7773f5b18893decde668fbc001f7ae",
    dependency_scope="identical package-lock; reuse isolated npm ci dependencies (TypeScript5.9.3), JDK17/SDK/Gradle caches; no fresh dependency install",
    device_verified=False)
apk = snapshot / "android-app/android/app/build/outputs/apk/debug/app-debug.apk"
if results[-1]["exit_code"] == 0:
    record["apk"] = dict(path=str(apk), sha256=hashlib.sha256(apk.read_bytes()).hexdigest(), size=apk.stat().st_size,
        installed=False, js_delivery="debug APK; JS requires Metro from the matching source")
(out / "committed-evidence.json").write_text(json.dumps(record, indent=2), encoding="utf-8", newline="\n")
raise SystemExit(0 if all(r["exit_code"] == 0 for r in results) else 1)
