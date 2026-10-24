"""Commit only the explicitly enumerated runtime review metadata."""
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2]


def git(*args):
    return subprocess.check_output(["git", *args], cwd=root)


assert not git("diff", "--cached", "--name-only").strip()
assert git("rev-parse", "HEAD").decode().strip() == "b69e9aabc59fec96285c9b2575ca29110178ee26", "Inspect concurrent commits before staging"
prefix = ".planning/2026-10-24-plan-runtime-review/"
paths = [prefix + name for name in (
    "task_plan.md", "progress.md", "findings.md", "verify_before.py", "verify_committed.py", "verify_related.py", "verify_metadata.py",
    "finalize_review.py", "reconcile_peer_acceptance.py", "commit_review.py", "before-evidence.json", "committed-evidence.json", "related-evidence.json",
    "first-attempt-e54ad2a.json", "second-attempt-eea3a00.json", "before-committed.txt", "related-final.txt", "related-unbound-probe.txt",
)]
for suffix in ("", "-e54ad2a", "-eea3a00"):
    paths += [prefix + name + suffix + ".txt" for name in (
        "backend-committed", "claims-committed", "ledgers-committed", "board-committed", "gap-committed", "alignment-committed")]
paths += ["docs/协作开发/" + name for name in (
    "README.md", "workboard.json", "开发任务认领表.md", "验收/2026-10-24-ALN-07-P运行时补审.md",
    "交付/ALN-07-P-计划修订与证据保持.md", "任务卡/07-计划修订与证据保持.md",
    "交付/ALN-07-E-副作用账本对账.md", "任务卡/07-E副作用账本对账.md",
)]
paths += ["docs/项目现状与审阅进展_2026-10-24.md", "docs/项目现状与设计差距核对_2026-10-23.md"]
subprocess.run(["git", "add", "--", *paths], cwd=root, check=True)
subprocess.run(["git", "commit", "-m", "docs: record runtime plan review repairs and pinned verification"], cwd=root, check=True)
