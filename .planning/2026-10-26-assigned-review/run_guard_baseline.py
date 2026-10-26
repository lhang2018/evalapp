"""Run newly added probes over the earlier committed business source."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
commit = "a435ce14ad0ef175a73fa1a8b628fbde31c047b6"
probe = "tests/test_assigned_scheduler_review_20261026.py"
record = {"source_commit": commit, "test_overlay": probe}
with tempfile.TemporaryDirectory(prefix="assigned-guard-negative-", dir=root / "outputs") as temp:
    owned = Path(temp)
    assert owned.resolve().parent == (root / "outputs").resolve()
    record["owned_temp_directory"] = str(owned)
    archive = owned / "source.zip"
    subprocess.run(["git", "archive", "--format=zip", "--output="+str(archive), commit], cwd=root, check=True)
    record["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    source = owned / "source"
    with zipfile.ZipFile(archive) as bundle:
        assert all(not Path(n).is_absolute() and ".." not in Path(n).parts for n in bundle.namelist())
        bundle.extractall(source)
    data = (root / probe).read_bytes()
    (source / probe).write_bytes(data)
    record["test_sha256_lf"] = hashlib.sha256(data.replace(b"\r\n",b"\n")).hexdigest()
    scratch = owned / "scratch"
    scratch.mkdir()
    env = dict(os.environ, PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
               TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch))
    command = [sys.executable,"-X","utf8","-m","pytest",probe,"-k",
               "real_app_scheduler_consumes_off or api_does_not_coerce", "-q", "-p","no:cacheprovider",
               "--basetemp="+str(scratch/"pytest")]
    record["command"] = command
    with (folder/"guard-negative-tests.txt").open("w",encoding="utf-8") as log:
        result = subprocess.run(command,cwd=source,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=60)
    record["exit_code"] = result.returncode
record["temporary_data_removed"] = not owned.exists()
(folder/"guard-negative-validation.json").write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print("baseline exit:", record["exit_code"], "scope removed:",record["temporary_data_removed"])
raise SystemExit(0 if record["exit_code"] == 1 else 1)
