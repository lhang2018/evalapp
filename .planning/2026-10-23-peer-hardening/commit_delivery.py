"""Commit this delivery's metadata without collecting another session's claims."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
os.chdir(root)

def git(*args, **kwargs):
    return subprocess.check_output(["git", *args], **kwargs)

assert not git("diff", "--cached", "--name-only").strip(), "Preserve another session's staging"
board_path = "docs/协作开发/workboard.json"
base = json.loads(git("show", "HEAD:" + board_path))
working = json.loads(Path(board_path).read_text(encoding="utf-8"))
ids = set(working["baseline"]["latest_repair_followup"]["cards"])
for target in base["dispatches"]:
    if target["id"] in ids:
        target.update(next(item for item in working["dispatches"] if item["id"] == target["id"]))
parents = {item["workpackage"] for item in working["dispatches"] if item["id"] in ids}
for target in base["workpackages"]:
    if target["id"] in parents:
        current = next(item for item in working["workpackages"] if item["id"] == target["id"])
        for field in ("next_step", "delivery_state"):
            target[field] = current[field]
for field in ("latest_source_verification", "latest_repair_followup"):
    base["baseline"][field] = working["baseline"][field]
base["updated_at"] = working["updated_at"]
for path in ["evoagent/planning/contracts.py"]:
    if not any(item["path"] == path for item in base["shared_files"]):
        base["shared_files"].append(next(item for item in working["shared_files"] if item["path"] == path))

spec = importlib.util.spec_from_file_location("board_checker", root / "tools/check_collaboration_plan.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)
rendered = checker.render(base)
with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", suffix=".md",
                                 dir=root / "outputs", delete=False) as tmp:
    tmp.write(rendered)
    rendered_path = Path(tmp.name)
try:
    checker.RENDERED = rendered_path
    assert not checker.validate(base), checker.validate(base)
finally:
    rendered_path.unlink()

prefix = ".planning/2026-10-23-peer-hardening/"
files = ["docs/协作开发/验收/2026-10-23-审核修复后续加固.md"] + [prefix + name for name in (
    "task_plan.md", "findings.md", "progress.md", "verify_before.py", "verify_committed.py",
    "finalize_review.py", "verify_metadata.py", "commit_delivery.py", "committed-evidence.json", "supplemental-evidence.json",
    "backend-committed.txt", "claims-committed.txt", "ledgers-committed.txt", "board-committed.txt",
    "before-fixed.txt", "related-final.txt")]
index = root / prefix / "delivery.index"
env = {**os.environ, "GIT_INDEX_FILE": str(index)}
subprocess.run(["git", "read-tree", "HEAD"], env=env, check=True)
subprocess.run(["git", "add", "--", *files], env=env, check=True)
for path, content in ((board_path, json.dumps(base, ensure_ascii=False, indent=2) + "\n"),
                      ("docs/协作开发/开发任务认领表.md", rendered)):
    blob = git("hash-object", "-w", "--stdin", input=content.encode()).decode().strip()
    subprocess.run(["git", "update-index", "--add", "--cacheinfo", "100644", blob, path], env=env, check=True)
subprocess.run(["git", "commit", "-m", "docs: record follow-up recovery and workflow hardening evidence"], env=env, check=True)
# Keep all working-tree content, including other sessions' new claims, intact.
subprocess.run(["git", "reset", "--quiet", "HEAD", "--", *files, board_path,
                "docs/协作开发/开发任务认领表.md"], check=True)
index.unlink()
