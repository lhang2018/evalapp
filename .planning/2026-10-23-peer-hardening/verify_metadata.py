"""Check committed evidence bytes and collaboration metadata in a fresh Git archive."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile
root=Path(__file__).resolve().parents[2]
def git(*args):
    return subprocess.check_output(["git", *args], cwd=root)
commit=git("rev-parse", sys.argv[1] if len(sys.argv)>1 else "HEAD").decode().strip()
prefix=".planning/2026-10-23-peer-hardening/"
count=0
for name in ("committed-evidence.json", "supplemental-evidence.json"):
    evidence=json.loads(git("show", commit+":"+prefix+name))
    for entry in evidence.get("results", evidence.get("logs", [])):
        path=entry.get("log", entry.get("path")).replace("\\", "/")
        content=git("show", commit+":"+path)
        assert hashlib.sha256(content).hexdigest()==entry["sha256"], path
        count+=1
    if "source_commit" in evidence:
        source=evidence["source_commit"]
        assert git("rev-parse", source+"^{tree}").decode().strip()==evidence["source_tree"]
        paths=["evoagent/planning/contracts.py", "evoagent/planning/matching.py",
               "evoagent/evolution/routing_eval.py", "evoagent/tools/coding.py",
               "tests/test_peer_hardening.py", "tests/test_coding_tools.py"]
        assert not git("diff", source, commit, "--", *paths).strip()
snapshot=root/"outputs"/("peer-hardening-metadata-"+commit[:7])
snapshot.mkdir(exist_ok=False)
with zipfile.ZipFile(io.BytesIO(git("archive", "--format=zip", commit))) as archive:
    archive.extractall(snapshot)
def check(command):
    result=subprocess.run(command, cwd=snapshot, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode==0, result.stdout+result.stderr
    return dict(command=command, exit_code=result.returncode)
with ThreadPoolExecutor(3) as pool:
    checks=list(pool.map(check, [[sys.executable, "tools/check_collaboration_plan.py", "--verify-prepared"],
                [sys.executable, "tools/verify_claims.py", "--check"], [sys.executable, "tools/scan_all_ledgers.py"]]))
print(json.dumps(dict(metadata_commit=commit, verified_git_blob_hashes=count, checks=checks), ensure_ascii=False, indent=2))
