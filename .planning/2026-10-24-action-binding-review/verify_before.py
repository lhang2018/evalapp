"""Reproduce independent probes against the immutable original implementation."""
import hashlib
import io
import json
from pathlib import Path
import os
import subprocess
import sys
import zipfile

root = Path(__file__).resolve().parents[2]
evidence = Path(__file__).resolve().parent
ref = subprocess.check_output(["git", "rev-parse", "ea5fc15"], cwd=root, text=True).strip()
snapshot = root / "outputs" / ("action-binding-review-before-" + ref[:7])
snapshot.mkdir(parents=True, exist_ok=False)
archive = subprocess.check_output(["git", "archive", "--format=zip", ref], cwd=root)
with zipfile.ZipFile(io.BytesIO(archive)) as z:
    z.extractall(snapshot)
probe = "tests/test_peer_action_binding_review.py"
(snapshot / probe).write_bytes((root / probe).read_bytes())
env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
command = [sys.executable, "-m", "pytest", probe, "-q", "--tb=short"]
log = evidence / "before-committed.txt"
with log.open("w", encoding="utf-8", newline="\n") as output:
    result = subprocess.run(command, cwd=snapshot, env=env, stdout=output, stderr=subprocess.STDOUT)
log.write_text(log.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
record = dict(source_commit=ref, injected_probe=probe,
              probe_sha256=hashlib.sha256((root / probe).read_bytes()).hexdigest(),
              command=command, exit_code=result.returncode,
              log=str(log.relative_to(root)), sha256=hashlib.sha256(log.read_bytes()).hexdigest())
(evidence / "before-evidence.json").write_text(json.dumps(record, indent=2), encoding="utf-8", newline="\n")
print(log.read_text(encoding="utf-8")[-1800:])
