"""Register observed source deltas without accepting them or changing the candidate."""
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2]
path = root / "docs/协作开发/workboard.json"
board = json.loads(path.read_text(encoding="utf-8"))
verification = json.loads((Path(__file__).parent / "verification.json").read_text(encoding="utf-8"))
commit = subprocess.check_output(["git", "rev-parse", "0b8d7dd"], cwd=root, text=True).strip()
files = subprocess.check_output(["git", "diff-tree", "--no-commit-id", "--name-only", "-z", "-r", commit,
    "--", "android-app"], cwd=root).decode().split("\0")
assert not board.get("post_candidate_changes"), "Inspect existing delta registration before editing"
board["post_candidate_changes"] = [dict(id="notification-transitions", title="任务终态跃迁通知与后台同步接线",
    workpackage="ALN-12", source_commit=commit, status="committed_pending_acceptance", files=[p for p in files if p],
    evidence=".planning/2026-10-25-new-additions/verification.json",
    report="docs/协作开发/验收/2026-10-25-新增提交识别与现状校准.md",
    scope="实际提交6个文件；新增8项客户端测试；本轮仅核对登记/自动化，未独立接受该功能或验证新原生通知APK",
    candidate_included=False, device_verified=False)]
board["baseline"]["latest_incremental_verification"] = dict(source_commit=verification["source_commit"],
    client_passed=236, typecheck="passed", evidence=".planning/2026-10-25-new-additions/verification.json",
    python_evidence_mode="reused_identical_backend_and_test_trees", python_source_commit="f550d9063f7773f5b18893decde668fbc001f7ae",
    python_passed=1661, python_skipped=1, source_tree=verification["source_tree"],
    scope="新HEAD客户端/类型检查实测；Python源码与tests相对f550d90零差异，后端1661/1沿用而非本轮重跑；不替换候选或扩大真机证明")
path.write_text(json.dumps(board, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
