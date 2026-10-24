"""Stage only the enumerated binding review metadata, never other sessions."""
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2]


def git(*args):
    return subprocess.check_output(["git", *args], cwd=root)


assert git("rev-parse", "HEAD") == git("rev-parse", "cc0aa3a"), "Inspect concurrent commits before staging"
assert not git("diff", "--cached", "--name-only").strip()
prefix = ".planning/2026-10-24-action-binding-review/"
paths = [prefix + name for name in (
    "task_plan.md", "progress.md", "findings.md", "verify_before.py", "verify_committed.py", "verify_metadata.py",
    "finalize_review.py", "commit_review.py", "before-evidence.json", "committed-evidence.json", "related-evidence.json",
    "before-committed.txt", "related-final.txt", "backend-committed.txt", "client-committed.txt", "typecheck-committed.txt",
    "claims-committed.txt", "ledgers-committed.txt", "board-committed.txt", "gap-committed.txt", "alignment-committed.txt",
)]
paths += ["docs/协作开发/" + name for name in (
    "README.md", "workboard.json", "开发任务认领表.md", "验收/2026-10-24-ALN-07-B独立审核.md",
    "交付/ALN-07-B-运行时动作绑定.md", "任务卡/07-B运行时动作绑定.md",
)]
paths += ["docs/项目现状与审阅进展_2026-10-24.md", "docs/项目现状与设计差距核对_2026-10-23.md"]
subprocess.run(["git", "add", "--", *paths], cwd=root, check=True)
subprocess.run(["git", "commit", "-m", "docs: record independent action binding review and current delivery status"], cwd=root, check=True)
