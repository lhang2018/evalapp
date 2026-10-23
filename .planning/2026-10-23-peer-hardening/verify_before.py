"""Replay the final independent probes against the unchanged pre-fix Git object."""
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile
root = Path(__file__).resolve().parents[2]
evidence = Path(__file__).resolve().parent
commit = subprocess.check_output(["git", "rev-parse", "8621c8e"], cwd=root, text=True).strip()
snapshot = root / "outputs" / "peer-hardening-before"
snapshot.mkdir(exist_ok=False)
with zipfile.ZipFile(io.BytesIO(subprocess.check_output(["git", "archive", "--format=zip", commit], cwd=root))) as archive:
    archive.extractall(snapshot)
shutil.copyfile(root / "tests/test_peer_hardening.py", snapshot / "tests/test_peer_hardening.py")
result = subprocess.run([sys.executable, "-m", "pytest", "tests/test_peer_hardening.py", "-q", "--tb=short"], cwd=snapshot,
    capture_output=True, env={**os.environ, "PYTHONUTF8":"1", "PYTHONIOENCODING":"utf-8"})
text = (result.stdout + result.stderr).decode("utf-8").replace("\r\n", "\n")
(evidence / "before-fixed.txt").write_text(text, encoding="utf-8", newline="\n")
print(text[-500:])
assert result.returncode == 1, "Expected regression assertions to fail on original source"
