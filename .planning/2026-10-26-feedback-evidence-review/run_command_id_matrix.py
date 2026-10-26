"""Isolate a deterministic command-id length failure from changing worktree logs."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

root = Path(__file__).resolve().parents[2]
evidence = Path(__file__).resolve().parent
commit = "6e8285632092e2fc1482417d432023134f7ba535"
record = {"commit": commit, "variants": {}, "historical_failure_classification":
          "Unfixed working-tree source is not a fixed-commit flakiness experiment"}
record["source_tree"] = subprocess.check_output(
    ["git", "rev-parse", commit + "^{tree}"], cwd=root, text=True).strip()
with tempfile.TemporaryDirectory(prefix="command-id-matrix-", dir=root / "outputs") as temp:
    owned = Path(temp)
    assert owned.resolve().parent == (root / "outputs").resolve()
    record["owned_temp_directory"] = str(owned)
    archive = owned / "source.zip"
    subprocess.run(["git", "archive", "--format=zip", "--output=" + str(archive), commit], cwd=root, check=True)
    record["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    source = owned / "source"
    with zipfile.ZipFile(archive) as bundle:
        assert all(not Path(n).is_absolute() and ".." not in Path(n).parts for n in bundle.namelist())
        bundle.extractall(source)
    app = source / "evoagent/app.py"
    original = app.read_text(encoding="utf-8")
    needle = "goal_command_id=hashlib.sha256("
    assert original.count(needle) == 1
    for name, text, expected in (
        ("prefix_plus_digest", original.replace(needle, 'goal_command_id="proactive_" + hashlib.sha256('), 1),
        ("fixed_digest_only", original, 0),
    ):
        app.write_text(text, encoding="utf-8")
        scratch = owned / name
        scratch.mkdir()
        env = dict(os.environ, PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1",
                   PYTHONUTF8="1", TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch),
                   SQLITE_TMPDIR=str(scratch))
        command = [sys.executable, "-X", "utf8", "-m", "pytest", "tests/test_proactive_execution.py",
                   "-k", "test_app_wires_attempt_to_authorized_goal_opportunity_task", "-q",
                   "-p", "no:cacheprovider", "--basetemp=" + str(scratch / "pytest")]
        with (evidence / (name + ".txt")).open("w", encoding="utf-8") as log:
            result = subprocess.run(command, cwd=source, env=env, stdout=log,
                                    stderr=subprocess.STDOUT, timeout=60)
        record["variants"][name] = {"command": command, "app_sha256_lf": hashlib.sha256(text.encode()).hexdigest(),
                                   "exit_code": result.returncode, "expected_exit_code": expected,
                                   "only_override": 'prepend "proactive_" to the 64-character command digest'
                                   if expected else "no override: exact fixed app source"}
record["temporary_data_removed"] = not owned.exists()
record["all_expected"] = all(v["exit_code"] == v["expected_exit_code"] for v in record["variants"].values())
(evidence / "command-id-matrix.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(record, ensure_ascii=False, indent=2))
raise SystemExit(0 if record["all_expected"] else 1)
