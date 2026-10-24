"""Commit only the reviewed baseline preparation and fixture-only device evidence."""
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2]


def git(*args):
    return subprocess.check_output(["git", *args], cwd=root)


assert git("rev-parse", "HEAD").strip() == git("rev-parse", "532a364").strip()
assert not git("diff", "--cached", "--name-only").strip(), "Index is not empty"
prefix = ".planning/2026-10-24-baseline-device/"
names = ["task_plan.md", "findings.md", "progress.md", "verify_candidate.py", "build_candidate.py",
    "make_fixtures.mjs", "deviceReview.tsx", "metro.device.config.js", "metro-mode.txt", "device_capture.py",
    "read_device_report.py", "attest_device.py", "prepare_metadata.py", "verify_delivery.py", "commit_delivery.py",
    "candidate-source.json", "candidate-validation.json", "build-evidence.json", "device-fixtures.json",
    "device-before-restart.json", "device-after-restart.json", "device-attestation.json", "force-stop-evidence.json",
    "metadata-working-checks.json", "served-harness-bundle.json", "backend.txt", "client.txt", "typecheck.txt", "android-build.txt",
    "claims.txt", "ledgers.txt", "board.txt", "gap.txt", "alignment.txt", "good.bundle", "bad.bundle", "timeout.bundle",
    "harness-first.png", "harness-first.xml", "harness-phase1.png", "harness-phase1.xml",
    "harness-after-restart.png", "harness-after-restart.xml"]
paths = [prefix + name for name in names] + ["docs/协作开发/workboard.json", "docs/协作开发/开发任务认领表.md",
    "docs/协作开发/README.md", "docs/协作开发/任务卡/00-基线发布.md", "docs/协作开发/协作规则与共享文件.md",
    "docs/项目现状与审阅进展_2026-10-24.md", "docs/项目现状与设计差距核对_2026-10-23.md",
    "docs/协作开发/交付/ALN-00-B-新候选与真机回归.md",
    "docs/项目全景与设计达成度评估_2026-10-24.md"]
# Another session swept the in-flight evidence directory into 532a364. Remove
# only our four product captures from current tracking; keep their private copy.
removed = [prefix + n for n in ("product-start.png", "product-start.xml", "product-return.png", "product-return.xml")]
assert all((root / path).is_file() for path in paths)
assert all(not (root / path).exists() for path in removed)
paths += removed
subprocess.run(["git", "add", "--", *paths], cwd=root, check=True)
staged = set(git("diff", "--cached", "--name-only", "-z").decode().split("\0")) - {""}
assert staged and staged.issubset(set(paths)), staged.difference(paths)
subprocess.run(["git", "diff", "--cached", "--check", "--", *[p for p in paths if not p.endswith(".txt")]], cwd=root, check=True)
subprocess.run(["git", "commit", "-m", "docs: prepare current baseline and verify candidate on Android device"], cwd=root, check=True)
print(git("rev-parse", "HEAD").decode().strip())
