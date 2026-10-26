"""Validate a committed client tree; remove only this run's new temporary data."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument("commit")
args = parser.parse_args()
root = Path(__file__).resolve().parents[2]
evidence = Path(__file__).resolve().parent
outputs = root / "outputs"
outputs.mkdir(exist_ok=True)
commit = subprocess.check_output(
    ["git", "rev-parse", args.commit + "^{commit}"], cwd=root, text=True
).strip()
record = {
    "commit": commit,
    "source_tree": subprocess.check_output(
        ["git", "rev-parse", commit + "^{tree}"], cwd=root, text=True
    ).strip(),
    "runner_sha256_lf": hashlib.sha256(
        Path(__file__).read_bytes().replace(b"\r\n", b"\n")
    ).hexdigest(),
    "node": subprocess.check_output(["node", "--version"], text=True).strip(),
}
exit_code = 1
with tempfile.TemporaryDirectory(prefix="device-app-", dir=outputs) as temporary:
    owned = Path(temporary)
    assert owned.resolve().parent == outputs.resolve()
    record["owned_temp_directory"] = str(owned)
    archive = owned / "client.zip"
    subprocess.run(
        ["git", "archive", "--format=zip", "--output=" + str(archive), commit, "android-app"],
        cwd=root, check=True,
    )
    record["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    source = owned / "source"
    with zipfile.ZipFile(archive) as bundle:
        assert all(not Path(name).is_absolute() and ".." not in Path(name).parts
                   for name in bundle.namelist())
        bundle.extractall(source)
    client = source / "android-app"
    scratch = owned / "scratch"
    scratch.mkdir()
    env = dict(os.environ, TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch),
               PYTHONDONTWRITEBYTECODE="1", GIT_CEILING_DIRECTORIES=str(scratch))
    record["tested_source"] = str(client)
    dependencies = root / "android-app" / "node_modules"
    junction = client / "node_modules"
    assert not junction.exists()
    assert junction.parent.resolve().is_relative_to(owned.resolve())
    def ps_quote(path):
        return "'" + str(path).replace("'", "''") + "'"
    try:
        # Resolve existing installed libraries from the exact archived source.
        # This newly created junction is unlinked before removing the owned temp.
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path "
             + ps_quote(junction) + " -Target " + ps_quote(dependencies) + " | Out-Null"],
            check=True, capture_output=True,
        )
        commands = {
            "client-tests": ["node", "--test", "--test-reporter=tap", "test/offline/*.test.ts",
                             "test/device/*.test.ts", "test/update/*.test.ts"],
            "typecheck": ["node", str(dependencies / "typescript/bin/tsc"), "--noEmit"],
        }
        record["typescript"] = json.loads(
            (dependencies / "typescript/package.json").read_text(encoding="utf-8")
        )["version"]
        record["commands"] = commands
        record["results"] = {}
        for name, command in commands.items():
            with (evidence / (name + ".txt")).open("w", encoding="utf-8") as log:
                result = subprocess.run(command, cwd=client, env=env, stdout=log,
                                        stderr=subprocess.STDOUT)
            record["results"][name] = result.returncode
        exit_code = int(any(record["results"].values()))
    finally:
        if junction.exists():
            assert junction.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            assert junction.resolve() == dependencies.resolve()
            os.rmdir(junction)  # Remove just our junction, never its target.
        record["dependency_junction_removed"] = not junction.exists()
        record["installed_dependencies_preserved"] = dependencies.is_dir()
record["temporary_data_removed"] = not Path(temporary).exists()
record["cleanup_scope"] = "only this run's new TemporaryDirectory and junction; no old caches touched"
(evidence / "validation.json").write_text(
    json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(json.dumps(record, ensure_ascii=False, indent=2))
raise SystemExit(exit_code)
