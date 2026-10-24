"""Reproduce independent probes against the immutable original implementation."""
import hashlib
import io
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import zipfile

root = Path(__file__).resolve().parents[2]
evidence = Path(__file__).resolve().parent
ref = subprocess.check_output(["git", "rev-parse", "50dd94c"], cwd=root, text=True).strip()
snapshot = Path(tempfile.mkdtemp(prefix="plan-runtime-review-before-" + ref[:7] + "-", dir=root / "outputs"))
archive = subprocess.check_output(["git", "archive", "--format=zip", ref], cwd=root)
with zipfile.ZipFile(io.BytesIO(archive)) as z:
    z.extractall(snapshot)
probes = ["tests/test_plan_runtime_review.py", "tests/test_auth_rejection_transport.py"]
for probe in probes:
    (snapshot / probe).write_bytes((root / probe).read_bytes())
env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
command = [sys.executable, "-m", "pytest", *probes, "-q", "--tb=short"]
log = evidence / "before-committed.txt"
with log.open("w", encoding="utf-8", newline="\n") as output:
    result = subprocess.run(command, cwd=snapshot, env=env, stdout=output, stderr=subprocess.STDOUT)
log.write_text(log.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
record = dict(source_commit=ref, injected_probes=[dict(path=probe,
              sha256=hashlib.sha256((root / probe).read_bytes()).hexdigest()) for probe in probes],
              command=command, exit_code=result.returncode,
              log=str(log.relative_to(root)), sha256=hashlib.sha256(log.read_bytes()).hexdigest())
(evidence / "before-evidence.json").write_text(json.dumps(record, indent=2), encoding="utf-8", newline="\n")
print(log.read_text(encoding="utf-8")[-1800:])
