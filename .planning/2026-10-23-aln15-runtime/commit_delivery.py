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
for target in base["dispatches"]:
    if target["id"] in {"ALN-14-A", "ALN-15-R"}:
        target.update(next(item for item in working["dispatches"] if item["id"] == target["id"]))
for target in base["workpackages"]:
    if target["id"] in {"ALN-14", "ALN-15"}:
        current = next(item for item in working["workpackages"] if item["id"] == target["id"])
        for field in ("next_step", "delivery_state"):
            target[field] = current[field]
base["baseline"]["latest_source_verification"] = working["baseline"]["latest_source_verification"]
for field in ("execution_note", "execution_queue", "updated_at"):
    base[field] = working[field]

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

prefix = ".planning/2026-10-23-aln15-runtime/"
files = [
    "docs/协作开发/README.md", "docs/项目现状与设计差距核对_2026-10-23.md",
    "docs/协作开发/任务卡/14-机会排序与无进展停止.md",
    "docs/协作开发/任务卡/15-预算预留运行时接线.md",
    "docs/协作开发/交付/ALN-15-R-运行时预算接线.md",
] + [prefix + name for name in (
    "task_plan.md", "findings.md", "progress.md", "verify_committed.py", "measure_history.py",
    "commit_delivery.py", "history-measurement.json", "committed-evidence.json", "committed-cc57ef6.json",
    "backend-committed.txt", "claims-committed.txt", "ledgers-committed.txt", "board-committed.txt",
    "backend-committed-cc57ef6.txt", "claims-committed-cc57ef6.txt",
    "ledgers-committed-cc57ef6.txt", "board-committed-cc57ef6.txt")]
index = root / prefix / "delivery.index"
env = {**os.environ, "GIT_INDEX_FILE": str(index)}
subprocess.run(["git", "read-tree", "HEAD"], env=env, check=True)
subprocess.run(["git", "add", "--", *files], env=env, check=True)
for path, content in ((board_path, json.dumps(base, ensure_ascii=False, indent=2) + "\n"),
                      ("docs/协作开发/开发任务认领表.md", rendered)):
    blob = git("hash-object", "-w", "--stdin", input=content.encode()).decode().strip()
    subprocess.run(["git", "update-index", "--add", "--cacheinfo", "100644", blob, path], env=env, check=True)
subprocess.run(["git", "commit", "-m", "docs: deliver ALN-15-R runtime budget wiring and verification"], env=env, check=True)
# Keep all working-tree content, including other sessions' new claims, intact.
subprocess.run(["git", "reset", "--quiet", "HEAD", "--", *files, board_path,
                "docs/协作开发/开发任务认领表.md"], check=True)
index.unlink()
