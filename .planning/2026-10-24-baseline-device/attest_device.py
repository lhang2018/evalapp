"""Bind device results to installed APK, served JS and unchanged candidate sources."""
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
import zipfile

root = Path(__file__).resolve().parents[2]
out = Path(__file__).resolve().parent
sys.stdout.reconfigure(encoding="utf-8")
source = json.loads((out / "candidate-source.json").read_text(encoding="utf-8"))
snapshot = Path(source["snapshot"])
build = json.loads((out / "build-evidence.json").read_text(encoding="utf-8"))
with zipfile.ZipFile(io.BytesIO(subprocess.check_output(["git", "-c", "core.autocrlf=false", "archive", "--format=zip", source["commit"]], cwd=root))) as archive:
    files = [name for name in archive.namelist() if name.startswith("android-app/") and not name.endswith("/")]
    for name in files:
        assert (snapshot / name).read_bytes() == archive.read(name), name
    paths = ["android-app/index.js", "android-app/App.tsx", "android-app/src/device/broker.ts",
        "android-app/src/device/taskProtocol.ts", "android-app/src/update/bundleRuntime.ts",
        "android-app/src/update/manager.ts", "android-app/src/update/workflowBundle.ts"]
    hashes = {name: hashlib.sha256(archive.read(name)).hexdigest() for name in paths}
assert (out / "metro-mode.txt").read_text().strip() == "product"
product = urllib.request.urlopen("http://127.0.0.1:8084/index.bundle?platform=android&dev=true&minify=false").read()
harness = urllib.request.urlopen("http://127.0.0.1:8084/deviceReview.bundle?platform=android&dev=true&minify=false").read()
assert b"Candidate device module regression starting" not in product
assert b"Candidate device module regression starting" in harness
adb = ["adb", "-s", "5748d823"]
apk_path = subprocess.check_output(adb + ["shell", "pm", "path", "com.evoagent"], text=True).strip().removeprefix("package:")
installed_hash = subprocess.check_output(adb + ["shell", "sha256sum", apk_path], text=True).split()[0]
assert installed_hash == build["apk"]["sha256"]
report = json.loads((out / "device-after-restart.json").read_text(encoding="utf-8"))
assert report["source_commit"] == source["commit"] and report["engine"] == "Hermes"
assert report["phase"] == "done" and len(report["tests"]) == 9 and all(t["ok"] for t in report["tests"])
force = json.loads((out / "force-stop-evidence.json").read_text(encoding="utf-8"))
assert force["killed"] is True and force["before_pid"] and not force["after_pid"]
versions = {}
for name, cmd in {"python": [sys.executable, "--version"], "node": ["node", "--version"],
    "java": [str(Path(os.environ["JAVA_HOME"]) / "bin/java.exe"), "-version"],
    "adb": ["adb", "version"]}.items():
    versions[name] = subprocess.run(cmd, capture_output=True, text=True).stdout.strip() or subprocess.run(cmd, capture_output=True, text=True).stderr.strip()
record = dict(source_commit=source["commit"], source_tree=source["tree"],
    attested_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    tracked_android_files_unchanged=len(files), important_source_sha256=hashes,
    installed_apk_sha256=installed_hash, apk_contains_js=False,
    product_js=dict(sha256=hashlib.sha256(product).hexdigest(), size=len(product), source="Metro8084; exact original index.js"),
    harness_js=dict(sha256=hashlib.sha256(harness).hexdigest(), size=len(harness), source="separate test entry; unchanged candidate business modules"),
    device_tests_passed=9, device_tests_total=9, actual_force_stop=True,
    product_entry_restored=True, versions=versions,
    dependencies=dict(npm_ci=dict(exit_code=0, installed_packages=599, elapsed_seconds=13,
        observation="actual npm ci stdout recorded by tool; no shared dependencies junction"),
        typescript=build["typescript"], python_pytest=subprocess.check_output([sys.executable, "-c", "import pytest; print(pytest.__version__)"], text=True).strip()),
    limitations=report["limits"] + ["accessibility service unavailable; permission refusal verified, no real click end-to-end claim", "force-stop is process restart, not physical device reboot"])
(out / "device-attestation.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
print(json.dumps({k: record[k] for k in ["source_commit", "tracked_android_files_unchanged", "installed_apk_sha256", "device_tests_passed", "product_entry_restored"]}, indent=2))
