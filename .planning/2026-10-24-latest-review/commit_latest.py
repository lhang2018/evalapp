"""Commit only this follow-up review; keep all unrelated work out of staging."""
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2]


def git(*args):
    return subprocess.check_output(["git", *args], cwd=root)


assert not git("diff", "--cached", "--name-only").strip()
assert git("rev-parse", "HEAD").decode().strip() == "6a4c0b570a3955fbe7558b28541ddb441c2e0d39", "Inspect concurrent commits before staging"
prefix = ".planning/2026-10-24-latest-review/"
paths = [prefix + name for name in ("verify_latest.py", "align_documents.py", "commit_latest.py", "evidence.json",
    "probe-before.txt", "probe-latest.txt", "related-latest.txt", "related-first-attempt.txt")]
paths += [".planning/2026-10-24-code-review/" + name for name in ("task_plan.md", "progress.md", "findings.md")]
paths += ["docs/协作开发/" + name for name in ("README.md", "workboard.json", "开发任务认领表.md",
    "验收/2026-10-24-三卡修复第三方复核.md", "验收/2026-10-24-新增三卡独立审核.md", "验收/2026-10-24-最新提交复核.md",
    "交付/ALN-07-T-轨迹编译.md", "交付/ALN-07-R-可执行恢复策略.md", "交付/ALN-16-X-导出下载与删除执行.md",
    "任务卡/07-T轨迹编译.md", "任务卡/07-R可执行恢复策略.md", "任务卡/16-X导出下载与删除执行.md")]
paths += ["docs/项目现状与审阅进展_2026-10-24.md", "docs/项目现状与设计差距核对_2026-10-23.md"]
subprocess.run(["git", "add", "--", *paths], cwd=root, check=True)
subprocess.run(["git", "commit", "-m", "docs: correct review evidence scope and propagate accepted status"], cwd=root, check=True)
