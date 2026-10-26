"""Fresh-process causal matrix in an owned Git archive, never the live checkout."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
SOURCE = "efe5ac0eccf1538eef086f94ce3c93452349e720"
record = dict(source_commit=SOURCE,source_tree=subprocess.check_output(
    ["git","rev-parse",SOURCE+"^{tree}"],cwd=ROOT,text=True).strip(),variants={})
output = ROOT/"outputs"
output.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix="windows-second-",dir=output) as tmp:
    owned = Path(tmp)
    record["owned_temp_directory"] = str(owned)
    archive = owned/"source.zip"
    subprocess.run(["git","archive","--format=zip","--output="+str(archive),SOURCE],cwd=ROOT,check=True)
    record["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    source = owned/"source"
    with zipfile.ZipFile(archive) as bundle:
        assert all(not Path(p).is_absolute() and ".." not in Path(p).parts for p in bundle.namelist())
        bundle.extractall(source)
    manager = source/"evoagent/release/manager.py"
    test = source/"tests/test_release_signing.py"
    original_manager, original_test = manager.read_text(encoding="utf-8"), test.read_text(encoding="utf-8")
    platform_line = '_SUPPORTED_PLATFORMS = ("android", "ios", "windows")'
    legacy_line = '("target_platforms", ["linux"], "target_platforms")'
    assert original_manager.count(platform_line) == original_test.count(legacy_line) == 1
    record["base_blobs"] = {"manager":hashlib.sha256(original_manager.encode()).hexdigest(),
                            "test":hashlib.sha256(original_test.encode()).hexdigest()}
    try:
        for name, legacy_assertion, reverted_platform, expected_code, expected_summary in (
            ("legacy_assertion_current_platform",True,False,1,"1 failed, 8 passed"),
            ("legacy_assertion_reverted_platform",True,True,0,"9 passed"),
            ("current_assertion_current_platform",False,False,0,"9 passed"),
            ("current_assertion_reverted_platform",False,True,0,"9 passed")):
            manager.write_text(original_manager.replace(platform_line,'_SUPPORTED_PLATFORMS = ("android", "ios")')
                               if reverted_platform else original_manager,encoding="utf-8")
            test.write_text(original_test.replace(legacy_line,'("target_platforms", ["windows"], "target_platforms")')
                            if legacy_assertion else original_test,encoding="utf-8")
            scratch = owned/name
            scratch.mkdir()
            env = dict(os.environ,PYTHONUTF8="1",PYTHONIOENCODING="utf-8",PYTHONDONTWRITEBYTECODE="1",
                PYTHONPATH=str(source),TEMP=str(scratch),TMP=str(scratch),TMPDIR=str(scratch),
                GIT_CEILING_DIRECTORIES=str(owned))
            command = [sys.executable,"-X","utf8","-m","pytest",
                "tests/test_release_signing.py::test_stable_channel_requires_complete_declarations",
                "-q","-p","no:cacheprovider","--basetemp="+str(scratch/"pytest")]
            log = OUT/("platform-"+name+".txt")
            with log.open("w",encoding="utf-8") as handle:
                result = subprocess.run(command,cwd=source,env=env,stdout=handle,stderr=subprocess.STDOUT)
            body = log.read_text(encoding="utf-8")
            record["variants"][name] = dict(command=command,legacy_assertion=legacy_assertion,
                reverted_platform=reverted_platform,exit_code=result.returncode,
                expected_exit_code=expected_code,expected_summary=expected_summary,
                matched=result.returncode==expected_code and expected_summary in body,
                manager_sha256=hashlib.sha256(manager.read_bytes()).hexdigest(),
                test_sha256=hashlib.sha256(test.read_bytes()).hexdigest())
    finally:
        manager.write_text(original_manager,encoding="utf-8")
        test.write_text(original_test,encoding="utf-8")
        record["isolated_files_restored"] = (manager.read_text(encoding="utf-8")==original_manager
                                             and test.read_text(encoding="utf-8")==original_test)
record["temporary_data_removed"] = not Path(tmp).exists()
record["all_variants_matched"] = all(r["matched"] for r in record["variants"].values())
(OUT/"platform-matrix.json").write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print(json.dumps({"all_variants_matched":record["all_variants_matched"],
    "temporary_data_removed":record["temporary_data_removed"],"source_commit":SOURCE},ensure_ascii=False))
raise SystemExit(0 if record["all_variants_matched"] else 1)
