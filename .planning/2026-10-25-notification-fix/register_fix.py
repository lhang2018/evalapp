"""Register the verified fix without changing candidate scope or acceptance."""
import copy
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
record = json.loads((folder / "committed-evidence.json").read_text(encoding="utf-8"))
assert all(r["exit_code"] == 0 for r in record["results"])
assert record["device_verified"] is False
path = root / "docs/协作开发/workboard.json"
board = json.loads(path.read_text(encoding="utf-8"))
assert len(board["post_candidate_changes"]) == 1
files = subprocess.check_output(["git", "diff-tree", "--no-commit-id", "--name-only", "-z", "-r", record["source_commit"],
    "--", "android-app"], cwd=root).decode().split("\0")
board["post_candidate_changes"].append(dict(id="notification-review-fix", title="通知回执、终态与后台同步可靠性修复",
    workpackage="ALN-12", source_commit=record["source_commit"], status="fixed_pending_device_validation",
    files=[p for p in files if p], author="codex-runtime", independent_review=False,
    evidence=".planning/2026-10-25-notification-fix/committed-evidence.json",
    report="docs/协作开发/验收/2026-10-25-通知功能修复与复验.md",
    scope="严格原生回执/权威终态/关闭channel/通知隔离/真实上传失败传播；16项新增探针，客户端252和类型检查、ARM64构建通过；未安装到真机",
    candidate_included=False, device_verified=False))
baseline = board["baseline"]
baseline.setdefault("incremental_verification_history", []).append(copy.deepcopy(baseline["latest_incremental_verification"]))
baseline["latest_incremental_verification"] = dict(source_commit=record["source_commit"], source_tree=record["source_tree"],
    client_passed=252, typecheck="passed", android_build="passed", device_verified=False,
    evidence=".planning/2026-10-25-notification-fix/committed-evidence.json",
    python_evidence_mode="reused_identical_backend_and_test_trees", python_source_commit=record["backend_evidence_reused_from"],
    python_passed=1661, python_skipped=1,
    scope="精确Git归档客户端252、TypeScript5.9.3与ARM64构建；后端同树沿用1661/1，非本轮重跑；原生通知实际投递待真机")
path.write_text(json.dumps(board, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
