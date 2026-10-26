"""Commit this review without absorbing another session's shared-board edits."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
board_path = "docs/协作开发/workboard.json"
report = "docs/协作开发/验收/2026-10-26-主动执行与原生资源采样复核.md"
owned = [
    str(folder.relative_to(root)).replace("\\", "/"), report, board_path,
    "docs/协作开发/交付/ALN-25-C-主动任务准入执行与真实资源非干扰.md",
    "docs/协作开发/交付/ALN-15-E-原生资源采样与后台周期准入.md",
    "docs/协作开发/待接需求_2026-10-26.md",
    "docs/协作开发/开发任务认领表.md",
]


def git(*args, env=None, **kwargs):
    return subprocess.check_output(["git", *args], cwd=root, env=env,
        text=True, encoding="utf-8", **kwargs).strip()


start = git("rev-parse", "HEAD")
assert not git("diff", "--cached", "--name-only", "--", *owned), "preserve other staged work"
base = json.loads(git("show", start + ":" + board_path))
working = json.loads((root / board_path).read_text(encoding="utf-8"))
cards = {c["id"]: c for c in working["dispatches"]}
fix = "6e8285632092e2fc1482417d432023134f7ba535"
for c in base["dispatches"]:
    if c["id"] not in {"ALN-25-C", "ALN-15-E"}:
        continue
    current = cards[c["id"]]
    for key in ("blocker", "next_step"):
        c[key] = copy.deepcopy(current[key])
    for evidence in current["evidence"]:
        if isinstance(evidence, dict) and evidence.get("source_commit") == fix:
            if evidence not in c["evidence"]:
                c["evidence"].append(copy.deepcopy(evidence))
for key, identity in (
    ("post_candidate_changes", "review-fix-" + fix[:12]),
    ("code_review_batches", "REVIEW-2026-10-26-PROACTIVE-NATIVE"),
):
    entry = next(c for c in working[key] if c["id"] == identity)
    if not any(c["id"] == identity for c in base[key]):
        base[key].append(copy.deepcopy(entry))

with tempfile.TemporaryDirectory(prefix="review-meta-", dir=root / "outputs") as temp:
    temp = Path(temp)
    assert temp.resolve().parent == (root / "outputs").resolve()
    env = dict(os.environ, GIT_INDEX_FILE=str(temp / "index"))
    git("read-tree", start, env=env)
    git("add", "--", *[p for p in owned if p != board_path], env=env)
    draft = temp / "workboard.json"
    draft.write_text(json.dumps(base, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    blob = git("hash-object", "-w", "--", str(draft))
    git("update-index", "--add", "--cacheinfo", "100644," + blob + "," + board_path, env=env)
    assert git("rev-parse", "HEAD") == start, "HEAD changed; rebuild from the new parent"
    assert not git("diff", "--cached", "--name-only", "--", *owned), "preserve new staged work"
    print(git("commit", "-m", "docs(review): 记录主动执行和原生采样修复及接受阻塞", env=env))
    # Update only these previously unstaged index paths to the new HEAD.
    # Foreign working-file hunks, including first_review metadata, stay intact.
    git("reset", "-q", "HEAD", "--", *owned)
print("own_metadata_staging_scope_removed", not temp.exists())
