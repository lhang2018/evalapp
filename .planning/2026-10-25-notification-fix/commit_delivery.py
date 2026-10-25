"""Stage only this fix's evidence and current-state handoff."""
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2]


def git(*args):
    return subprocess.check_output(["git", *args], cwd=root)


assert git("rev-parse", "HEAD").strip() == git("rev-parse", "271378e").strip()
assert not git("diff", "--cached", "--name-only").strip()
prefix = ".planning/2026-10-25-notification-fix/"
names = ["task_plan.md", "findings.md", "progress.md", "before-notifications.txt", "before-background.txt",
    "flush-before.txt", "targeted.txt", "verify_before.py", "before-committed.txt", "before-evidence.json",
    "verify_commit.py", "client.txt", "typecheck.txt", "android-build.txt", "committed-evidence.json",
    "register_fix.py", "verify_delivery.py", "commit_delivery.py"]
paths = [prefix + name for name in names] + ["tools/check_collaboration_plan.py", "docs/协作开发/workboard.json",
    "docs/协作开发/开发任务认领表.md", "docs/协作开发/README.md", "docs/协作开发/任务卡/00-基线发布.md",
    "docs/项目现状与审阅进展_2026-10-24.md", "docs/项目现状与设计差距核对_2026-10-23.md",
    "docs/项目全景与设计达成度评估_2026-10-24.md", "docs/协作开发/验收/2026-10-25-新增提交识别与现状校准.md",
    "docs/协作开发/验收/2026-10-25-通知功能修复与复验.md"]
assert all((root / path).is_file() for path in paths)
subprocess.run(["git", "add", "--", *paths], cwd=root, check=True)
staged = set(git("diff", "--cached", "--name-only", "-z").decode().split("\0")) - {""}
assert staged == set(paths), staged.symmetric_difference(paths)
subprocess.run(["git", "diff", "--cached", "--check", "--", *[p for p in paths if not p.endswith(".txt")]], cwd=root, check=True)
subprocess.run(["git", "commit", "-m", "docs: record notification fixes and verified Android build"], cwd=root, check=True)
print(git("rev-parse", "HEAD").decode().strip())
