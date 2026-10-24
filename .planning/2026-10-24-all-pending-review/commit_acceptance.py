"""Stage only the unified acceptance metadata; refuse a changed HEAD/index."""
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2]


def git(*args):
    return subprocess.check_output(["git", *args], cwd=root)


assert git("rev-parse", "HEAD").strip() == git("rev-parse", "eaa1342").strip(), "HEAD changed; inspect before committing"
existing_index = set(git("diff", "--cached", "--name-only", "-z").decode().split("\0")) - {""}
prefix = ".planning/2026-10-24-all-pending-review/"
planning = ["task_plan.md", "findings.md", "progress.md", "verify_committed.py", "verify_before.py",
    "audit_device_evidence.py", "device-evidence-audit.json", "finalize_acceptance.py",
    "committed-evidence.json", "before-evidence.json", "backend-committed.txt", "client-committed.txt",
    "typecheck-committed.txt", "claims-committed.txt", "ledgers-committed.txt", "board-committed.txt",
    "gap-committed.txt", "alignment-committed.txt", "runtime-before-committed.txt", "client-before-committed.txt",
    "verify_metadata.py", "commit_acceptance.py"]
paths = [prefix + name for name in planning] + [
    "tools/check_collaboration_plan.py",
    "docs/协作开发/workboard.json",
    "docs/协作开发/开发任务认领表.md",
    "docs/协作开发/README.md",
    "docs/协作开发/协作规则与共享文件.md",
    "docs/项目现状与审阅进展_2026-10-24.md",
    "docs/项目现状与设计差距核对_2026-10-23.md",
    "docs/协作开发/验收/2026-10-24-全部待审核统一验收.md",
    "docs/协作开发/任务卡/04-Android审批.md",
    "docs/协作开发/任务卡/07-B运行时动作绑定.md",
    "docs/协作开发/任务卡/11-活动工作流执行与健康探针.md",
    "docs/协作开发/任务卡/12-自然语言手机任务与离线可靠性.md",
    "docs/协作开发/任务卡/20-Runtime适配与Harness PoC.md",
    "docs/协作开发/交付/ALN-04-UI-管理员审批页面.md",
    "docs/协作开发/交付/ALN-07-B-运行时动作绑定.md",
    "docs/协作开发/交付/ALN-11-W-活动workflow执行消费与健康探针.md",
    "docs/协作开发/交付/ALN-12-A-自然语言任务设备协议与故障矩阵.md",
    "docs/协作开发/交付/ALN-20-A-Runtime适配协议与固定版本PoC设计.md",
]
assert all((root / path).is_file() for path in paths)
assert not existing_index or existing_index == set(paths), "Existing index contains unexpected paths"
subprocess.run(["git", "add", "--", *paths], cwd=root, check=True)
staged = set(git("diff", "--cached", "--name-only", "-z").decode().split("\0")) - {""}
assert staged == set(paths), ("Unexpected staged paths", staged.symmetric_difference(paths))
# Preserve raw test-output whitespace and its recorded hashes.
subprocess.run(["git", "diff", "--cached", "--check", "--",
                *[p for p in paths if not p.endswith(".txt")]], cwd=root, check=True)
subprocess.run(["git", "commit", "-m", "docs: accept all pending cards with scoped revalidation evidence"], cwd=root, check=True)
print(git("rev-parse", "HEAD").decode().strip())
