"""Find post-candidate source changes or task cards missing from the workboard.

This checks discovery/registration, never feature acceptance or publication.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess

SOURCE_ROOTS = ("evoagent", "android-app", "tests")


def check(root: Path, head_ref: str = "HEAD"):
    # A source archive can be inside another repository. Git pathspecs must be
    # rooted at the object repository, while card files belong to the archive.
    repository = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], cwd=root).decode().strip())

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repository)

    board = json.loads((root / "docs/协作开发/workboard.json").read_text(encoding="utf-8"))
    base = board["baseline"]["prepared_candidate"]["commit"]
    head = git("rev-parse", head_ref).decode().strip()
    changed = set(git("diff", "--name-only", "-z", base, head, "--", *SOURCE_ROOTS).decode().split("\0")) - {""}
    covered, errors = set(), []
    for entry in board.get("post_candidate_changes", []):
        commit = entry.get("source_commit", "")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            errors.append("change entry needs an exact source commit: " + entry.get("id", "?"))
            continue
        ancestor = subprocess.run(["git", "merge-base", "--is-ancestor", commit, head], cwd=repository, capture_output=True)
        if ancestor.returncode:
            errors.append("change entry is not part of HEAD: " + entry.get("id", "?"))
            continue
        committed_files = set(git("diff-tree", "--no-commit-id", "--name-only", "-z", "-r", "--root", commit,
                                  "--", *SOURCE_ROOTS).decode().split("\0")) - {""}
        for path in entry.get("files", []):
            if path not in committed_files:
                errors.append("entry lists a path absent from its commit: " + path)
                continue
            # A later unregistered edit to the same filename must also be found.
            if path in changed and git("ls-tree", "-z", commit, "--", path) == git("ls-tree", "-z", head, "--", path):
                covered.add(path)
    directory = root / "docs/协作开发/任务卡"
    cards = {path.relative_to(root).as_posix() for path in directory.rglob("*.md") if path.is_file()}
    registered = {card["card"] for card in board["dispatches"]}
    unknown = sorted(cards - registered)
    missing = sorted(registered - cards)
    uncovered = sorted(changed - covered)
    errors += ["unregistered source change: " + path for path in uncovered]
    errors += ["unregistered task card: " + path for path in unknown]
    errors += ["missing task card: " + path for path in missing]
    return dict(scope="committed source and task-card registration only; not acceptance", head=head,
        prepared_candidate=base, changed_files=sorted(changed), recorded_files=sorted(covered),
        unregistered_files=uncovered, unregistered_task_cards=unknown, missing_task_cards=missing,
        errors=errors, ok=not errors)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--head", default="HEAD", help="exact source revision to check, useful for archived metadata")
    args = parser.parse_args()
    result = check(args.root.resolve(), args.head)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
