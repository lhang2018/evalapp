"""Check design-source counts, links and unchanged task metadata; no business tests."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
def read(name):
    return json.loads((folder / name).read_text(encoding="utf-8"))

audit, mapping = read("corpus-audit.json"), read("design-mapping.json")
assert (audit["unique_messages"], audit["user_messages"], audit["user_exchanges"], audit["final_messages"]) == (470, 67, 65, 63)
assert audit["users_without_final"] == [119, 130]
assert audit["shared_exchange_groups"] == [[114, 115, 116]]
assert not audit["content_variant_ids"]
assert read("draft-classification.json")["classes"] == {
    "not_containing_corresponding_final": 2, "exact": 21,
    "contains_final_with_extra": 42, "no_final_in_source": 2,
}
assert len(mapping["rows"]) == 47
assert len({r["message_id"] for r in mapping["rows"]} |
           {r["message_id"] for r in mapping["product_discussions"]}) == 63
for item in audit["source_files"]:
    assert hashlib.sha256((root / item["path"]).read_bytes()).hexdigest() == item["sha256"]
archive = read("archive-verification.json")
assert archive["verified"] == archive["files"] == 44 and not archive["errors"]
ux = (root / "chat/extracted/156_qa.md").read_text(encoding="utf-8")
cases = (root / "chat/extracted/157_qa.md").read_text(encoding="utf-8")
assert len(set(re.findall(r"UX-\d{2}(?!\d)", ux))) == 28
assert len(set(re.findall(r"UX-(?:F0[1-4]|GLOBAL)-\d{2}", cases))) == 17

files = [
    "README.md", "chat/README.md", "chat/extracted/专项总目_2026-10-25.md",
    "chat/原稿归档/README.md", "docs/对话语料库对账与缺口清单_2026-10-25.md",
    "docs/产品目标形态与实施路线_2026-10-25.md",
    "docs/README新增设计梳理与实施映射_2026-10-25.md",
]
# Check added prose links, not historical code blocks or inline command examples.
link_errors, links = [], 0
for relative in ["chat/README.md", files[-1]]:
    path = root / relative
    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
        if target.startswith(("http:", "https:", "#")):
            continue
        links += 1
        if not (path.parent / unquote(target.split("#")[0])).is_file():
            link_errors.append(f"{relative}: {target}")
assert not link_errors, link_errors

board = root / "docs/协作开发/workboard.json"
tracked_board = subprocess.check_output(["git", "show", "HEAD:docs/协作开发/workboard.json"], cwd=root)
assert json.loads(board.read_text(encoding="utf-8")) == json.loads(tracked_board)
assert not subprocess.check_output(["git", "diff", "--name-only", "HEAD", "--", "evoagent", "android-app", "tests"], cwd=root)

checks = [
    ("collaboration", ["tools/check_collaboration_plan.py"]),
    ("current", ["tools/check_collaboration_plan.py", "--verify-current"]),
    ("prepared", ["tools/check_collaboration_plan.py", "--verify-prepared"]),
    ("source_coverage", ["tools/check_source_coverage.py"]),
    ("gap_mapping", ["tools/check_gap_closure.py"]),
    ("historical_snapshot", ["tools/check_collaboration_plan.py", "--verify-snapshot"]),
]
results = []
for name, arguments in checks:
    result = subprocess.run([sys.executable, "-X", "utf8", *arguments], cwd=root,
                            capture_output=True, text=True, encoding="utf-8")
    (folder / f"check-{name}.txt").write_text(result.stdout + result.stderr, encoding="utf-8", newline="\n")
    results.append(dict(name=name, returncode=result.returncode,
                        log=f".planning/2026-10-25-readme-design-review/check-{name}.txt"))
    if name != "historical_snapshot":
        assert result.returncode == 0, (name, result.stdout, result.stderr)
head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
verification = dict(head=head, source_counts_verified=True, archive_files_verified=44,
    numbered_designs=47, product_discussions=16, ux=28, acceptance_cases=17,
    links_verified=links, workboard_unchanged=True, business_source_unchanged=True,
    business_tests_rerun=False, checks=results,
    historical_snapshot_note="Historical frozen snapshot differs from current source; not a current-candidate readiness result.")
(folder / "verification.json").write_text(json.dumps(verification, ensure_ascii=False, indent=2),
                                         encoding="utf-8", newline="\n")
print(json.dumps(verification, ensure_ascii=False, indent=2))
