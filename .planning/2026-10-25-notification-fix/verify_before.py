"""Inject immutable final probes into the unchanged pre-fix Git archive."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

root = Path(__file__).resolve().parents[2]
out = Path(__file__).resolve().parent
sys.stdout.reconfigure(encoding="utf-8")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=root)


source = git("rev-parse", sys.argv[1]).decode().strip()
before = git("rev-parse", "cb813f3").decode().strip()
snapshot = root / "outputs/notification-before-cb813f3"
snapshot.mkdir(exist_ok=False)
with zipfile.ZipFile(io.BytesIO(git("-c", "core.autocrlf=false", "archive", "--format=zip", before))) as archive:
    archive.extractall(snapshot)
probes = []
for path in ("android-app/test/device/notificationReview.test.ts", "android-app/test/offline/backgroundNotificationReview.test.ts"):
    data = git("show", source + ":" + path)
    (snapshot / path).write_bytes(data)
    probes.append(dict(path=path, sha256=hashlib.sha256(data).hexdigest()))
command = ["node", "--test", *[p["path"] for p in probes]]
result = subprocess.run(command, cwd=snapshot, capture_output=True, text=True, encoding="utf-8")
log = out / "before-committed.txt"
log.write_text(result.stdout + result.stderr, encoding="utf-8", newline="\n")
assert result.returncode == 1
assert "pass 1" in result.stdout and "fail 15" in result.stdout
record = dict(before_commit=before, probe_source_commit=source, command=command, probes=probes,
    exit_code=result.returncode, passed=1, failed=15, log=log.relative_to(root).as_posix(),
    sha256=hashlib.sha256(log.read_bytes()).hexdigest(),
    scope="actual background assembly; native ports and HTTP explicit fixtures; original author tests unchanged")
(out / "before-evidence.json").write_text(json.dumps(record, indent=2), encoding="utf-8", newline="\n")
print(json.dumps(dict(passed=1, failed=15, before_commit=before), indent=2))
