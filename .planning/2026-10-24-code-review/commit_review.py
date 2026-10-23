"""Stage this review's exact files; refuse concurrent HEAD/staging changes."""
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2]


def git(*args):
    return subprocess.check_output(["git", *args], cwd=root)


assert not git("diff", "--cached", "--name-only").strip(), "Preserve existing staging"
assert git("rev-parse", "HEAD").decode().strip() == "f633130b8968079654405461fc6df379267e3917", "Inspect concurrent commits first"
prefix = ".planning/2026-10-24-code-review/"
files = [prefix + name for name in (
    "task_plan.md", "findings.md", "progress.md", "inventory.json", "before-evidence.json",
    "committed-evidence.json", "related-evidence.json", "before-committed.txt", "related-final.txt",
    "backend-committed.txt", "claims-committed.txt", "ledgers-committed.txt", "board-committed.txt",
    "gap-committed.txt", "alignment-committed.txt", "verify_before.py", "verify_committed.py",
    "verify_metadata.py", "finalize_review.py", "commit_review.py",
)] + [
    "docs/项目现状与审阅进展_2026-10-24.md", "docs/项目现状与设计差距核对_2026-10-23.md",
    "docs/协作开发/README.md", "docs/协作开发/workboard.json", "docs/协作开发/开发任务认领表.md",
    "docs/协作开发/验收/2026-10-24-新增三卡独立审核.md",
    "docs/协作开发/交付/ALN-07-T-轨迹编译.md", "docs/协作开发/交付/ALN-07-R-可执行恢复策略.md",
    "docs/协作开发/交付/ALN-16-X-导出下载与删除执行.md",
    "docs/协作开发/任务卡/07-T轨迹编译.md", "docs/协作开发/任务卡/07-R可执行恢复策略.md",
    "docs/协作开发/任务卡/16-X导出下载与删除执行.md",
]
subprocess.run(["git", "add", "--", *files], cwd=root, check=True)
subprocess.run(["git", "commit", "-m", "docs: reconcile completed tasks and record three-card review"], cwd=root, check=True)
