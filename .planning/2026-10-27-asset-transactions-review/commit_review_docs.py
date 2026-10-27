"""Commit only this batch's fields; leave concurrent board/card edits uncommitted."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
board_path = "docs/协作开发/workboard.json"
rendered_path = "docs/协作开发/开发任务认领表.md"
owned = [str(folder.relative_to(root)).replace("\\", "/"),
    "docs/协作开发/验收/2026-10-27-资产仓储共享事务复核.md", board_path, rendered_path,
    "docs/协作开发/待接需求_2026-10-26.md",
    "docs/协作开发/交付/ALN-06-A-不可变资产基础.md",
    "docs/协作开发/交付/ALN-27-B-可组合能力与动态工作流资产.md"]


def git(*args, env=None):
    return subprocess.check_output(["git", *args], cwd=root, env=env, text=True, encoding="utf-8").strip()


start = git("rev-parse", "HEAD")
assert not git("diff", "--cached", "--name-only", "--", *owned), "preserve foreign staged work"
original = json.loads(git("show", start + ":" + board_path))
clean = copy.deepcopy(original)
delta = json.loads((folder / "metadata-delta.json").read_text(encoding="utf-8"))
for card in clean["dispatches"]:
    item = delta["cards"].get(card["id"])
    if not item:
        continue
    assert card["status"] in ("accepted", "review")
    card.update(copy.deepcopy(item["fields"]))
    assert item["evidence"] not in card["evidence"]
    card["evidence"].append(item["evidence"])
    card.setdefault("review_history", []).append(item["history"])
for key in ("post_candidate_changes", "code_review_batches"):
    for item in delta[key]:
        assert not any(row["id"] == item["id"] for row in clean[key])
        clean[key].append(copy.deepcopy(item))
changed_cards = [card["id"] for card, old in zip(clean["dispatches"], original["dispatches"]) if card != old]
assert set(changed_cards) == set(delta["cards"])
spec = importlib.util.spec_from_file_location("assigned_board_check", root / "tools/check_collaboration_plan.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)
with tempfile.TemporaryDirectory(prefix="assigned-meta-", dir=root / "outputs") as temporary:
    scope = Path(temporary)
    assert scope.resolve().parent == (root / "outputs").resolve()
    draft_board, draft_rendered = scope / "workboard.json", scope / "board.md"
    draft_board.write_text(json.dumps(clean, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    draft_rendered.write_text(checker.render(clean), encoding="utf-8")
    checker.RENDERED = draft_rendered
    errors = checker.validate(clean) + checker.verify_prepared(clean)
    assert not errors, errors
    (folder / "isolated-metadata-check.json").write_text(json.dumps({
        "parent_commit": start, "changed_cards": changed_cards, "errors": errors,
        "workpackages": len(clean["workpackages"]), "dispatches": len(clean["dispatches"]),
        "working_board_not_replaced": True, "working_markdown_not_replaced": True,
        "note": "Only own 06-A/27-B review deltas reconstructed on HEAD; peer patch confirmation and all original reviewers/concurrent changes preserved."},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    env = dict(os.environ, GIT_INDEX_FILE=str(scope / "index"))
    git("read-tree", start, env=env)
    git("add", "--", *[path for path in owned if path not in (board_path, rendered_path)], env=env)
    for path, draft in ((board_path, draft_board), (rendered_path, draft_rendered)):
        blob = git("hash-object", "-w", "--", str(draft))
        git("update-index", "--add", "--cacheinfo", "100644," + blob + "," + path, env=env)
    assert git("rev-parse", "HEAD") == start, "HEAD advanced; rebuild own metadata from new parent"
    assert not git("diff", "--cached", "--name-only", "--", *owned), "preserve newly staged work"
    print(git("commit", "-m", "docs(review): 登记资产共享事务修复与二次确认", env=env))
    git("reset", "-q", "HEAD", "--", *owned)
print("isolated_metadata_scope_removed", not Path(temporary).exists())
current = json.loads((root / board_path).read_text(encoding="utf-8"))
committed = json.loads(git("show", "HEAD:" + board_path))
print("remaining_foreign_board_cards", [c["id"] for c, old in zip(current["dispatches"], committed["dispatches"]) if c != old])
