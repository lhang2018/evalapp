"""Run validation in a newly owned temporary scope and retain only evidence."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument("--record", required=True)
parser.add_argument("--snapshot")
args, pytest_args = parser.parse_known_args()
workspace = Path(__file__).resolve().parents[2]
outputs = workspace / "outputs"
outputs.mkdir(exist_ok=True)
record = {"pytest_arguments": pytest_args, "snapshot_commit": args.snapshot}
record["runner_sha256_lf"] = hashlib.sha256(
    Path(__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
result_code = 1
with tempfile.TemporaryDirectory(prefix="review5-", dir=outputs) as temporary:
    owned = Path(temporary)
    assert owned.resolve().parent == outputs.resolve()
    record["owned_temp_directory"] = str(owned)
    source = workspace
    if args.snapshot:
        archive = owned / "source.zip"
        subprocess.run(
            [
                "git",
                "archive",
                "--format=zip",
                "--output=" + str(archive),
                args.snapshot,
            ],
            cwd=workspace,
            check=True,
        )
        record["source_tree"] = subprocess.check_output(
            ["git", "rev-parse", args.snapshot + "^{tree}"], cwd=workspace, text=True
        ).strip()
        record["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        source = owned / "source"
        with zipfile.ZipFile(archive) as bundle:
            assert all(
                not Path(name).is_absolute() and ".." not in Path(name).parts
                for name in bundle.namelist()
            )
            bundle.extractall(source)
    scratch = owned / "scratch"
    scratch.mkdir()
    env = dict(
        os.environ,
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=str(source),
        TEMP=str(scratch),
        TMP=str(scratch),
        TMPDIR=str(scratch),
        SQLITE_TMPDIR=str(scratch),
        GIT_CEILING_DIRECTORIES=str(scratch),
    )
    record["source_path_when_tested"] = str(source)
    record["module_source_probe"] = subprocess.check_output(
        [sys.executable, "-X", "utf8", "-c",
         "import evoagent; import evoagent.tools.refactor as r; print(evoagent.__file__); print(r.__file__)"],
        cwd=source, env=env, text=True)
    result_code = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "pytest",
            *pytest_args,
            "-p",
            "no:cacheprovider",
            "--basetemp=" + str(scratch / "pytest"),
        ],
        cwd=source,
        env=env,
    ).returncode
    record["exit_code"] = result_code
record["temporary_data_removed"] = not Path(temporary).exists()
record["cleanup_scope"] = (
    "only the new TemporaryDirectory owned by this runner; no old cache roots touched"
)
(workspace / args.record).write_text(
    json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
sys.exit(result_code)
