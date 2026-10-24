"""Register the new verified candidate; do not silently grant baseline publication."""
import copy
import datetime
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2]
prefix = ".planning/2026-10-24-baseline-device/"
report = "docs/协作开发/交付/ALN-00-B-新候选与真机回归.md"
source = json.loads((root / prefix / "candidate-source.json").read_text(encoding="utf-8"))
validation = json.loads((root / prefix / "candidate-validation.json").read_text(encoding="utf-8"))
build = json.loads((root / prefix / "build-evidence.json").read_text(encoding="utf-8"))
device = json.loads((root / prefix / "device-attestation.json").read_text(encoding="utf-8"))
assert all(r["exit_code"] == 0 for r in validation["results"] + build["results"])
assert device["device_tests_passed"] == 9 and device["product_entry_restored"]
path = root / "docs/协作开发/workboard.json"
board = json.loads(path.read_text(encoding="utf-8"))
baseline = board["baseline"]
assert baseline["prepared_candidate"]["commit"].startswith("07f11e5")
assert baseline["published_commit"] is None and baseline["status"] == "candidate"
baseline.setdefault("prepared_candidate_history", []).append(copy.deepcopy(baseline["prepared_candidate"]))
evidence = [{"path": r["log"], "sha256": r["sha256"], "sha256_basis": r["sha256_basis"]}
    for r in validation["results"] + build["results"]]
baseline["prepared_candidate"] = dict(commit=source["commit"], source_tree=source["tree"],
    status="verified_ready_for_publication_decision", source_export="git archive --format=zip; project outputs; no new worktree",
    tracked_android_files_unchanged=device["tracked_android_files_unchanged"],
    python_passed=1661, python_skipped=1, client_passed=228, typecheck="passed",
    evidence=evidence, excluded_paths=baseline["prepared_candidate_history"][-1]["excluded_paths"],
    verified_by="codex-runtime", verified_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    review_mode="user_authorized_revalidation_not_independent", report=report,
    dependency_scope="Python requirements.txt installed environment; Node24 built-in tests; isolated npm ci lockfile; TypeScript5.9.3; JDK17/Android SDK/Gradle caches reused",
    device_evidence=prefix + "device-attestation.json", device_module_tests_passed=9,
    device_limits=device["limitations"], remaining_review=["00-B共享开发基线发布：原规则要求独立审阅；若采用本轮用户授权非独立复验发布，须明确确认该例外"],
    publication_ready=False, technical_verification_complete=True)
baseline["last_verified_candidate"] = dict(commit=source["commit"], source_tree=source["tree"],
    python_passed=1661, python_skipped=1, client_passed=228, typecheck="passed",
    scope="本项目内精确Git归档全量新跑；npm ci独立依赖与Android ARM64构建/安装及9项设备模块回归；不是第三方独立审阅",
    log=prefix + "backend.txt")
baseline["latest_source_verification"] = dict(source_commit=source["commit"], source_tree=source["tree"],
    python_passed=1661, python_skipped=1, client_passed=228, typecheck="passed",
    scope="新候选干净源码全量、独立npm ci与Android构建；最新APK+JS真机模块9项回归；正式App入口已恢复",
    evidence="../../" + prefix + "candidate-validation.json")
baseline.setdefault("acceptance_history", []).append(copy.deepcopy(baseline["latest_acceptance"]))
baseline["latest_acceptance"]["publication_ready"] = False
baseline["latest_acceptance"]["publication_blocker"] = "新候选f550d90及约定真机模块回归已齐；00-B正式发布仍待独立接受，或用户明确授权本次非独立复验发布"
baseline["latest_baseline_preparation"] = dict(source_commit=source["commit"], result="verified_ready_for_publication_decision",
    preparer="codex-runtime", independent=False, report=report, production_release=False)
card = next(c for c in board["dispatches"] if c["id"] == "ALN-00-B")
card.setdefault("ownership_history", []).append(dict(date="2026-10-24", **{"from": card["owner"], "to": "codex-runtime"},
    reason="用户同意新候选与真机回归，由当前会话完成00-B准备；不虚构独立发布接受"))
card.update(owner="codex-runtime", reviewer=None, status="review", base_commit=source["commit"],
    preparation_status="verified_ready_for_publication_decision", delivery=report,
    working_basis="当前项目内精确Git归档；f550d90为已验证待发布候选，尚非已发布共同基线",
    next_step="技术材料已齐；确认00-B发布采用独立审阅还是用户授权非独立复验，再登记published_commit；生产发布不在本轮范围")
card.pop("blocker", None)
card["evidence"].append(dict(date="2026-10-24", source_commit=source["commit"],
    scope="新候选补齐07466fb修复、f550d90证据校验；最新APK+JS真机回归；发布决定单列",
    command="python verify_candidate.py f550d90; npm ci; python build_candidate.py; ADB install/force-stop; deviceReview.tsx",
    result="1661 passed/1 skipped; client228; tsc5.9.3 passed; Android ARM64 built/installed; device module9/9",
    log=prefix + "candidate-validation.json", delivery=report))
queue = board.get("execution_queue")
if queue:
    board.setdefault("execution_queue_history", []).append(dict(**queue, archived_reason="初始队列已交付，00-B技术准备已齐转review；后续父包缺口待另拆卡"))
board["execution_queue"] = None
board["execution_note"] = "2026-10-24：初始开发及统一验收已交付；00-B新候选/真机模块回归完成，待发布决定。当前无实施卡在开发，后续优先补ALN-07原子绑定与对账，再打通ALN-12自然语言设备主链；不把尚未发布当所有开发的屏障。"
next(p for p in board["workpackages"] if p["id"] == "ALN-00")["next_step"] = "00-V接受；00-B新候选f550d90完成1661/1+228+tsc与9项真机模块回归，已解除旧候选缺修复的技术阻塞，待独立接受或明确授权非独立发布；整包未关闭"
path.write_text(json.dumps(board, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
subprocess.run(["python", "tools/check_collaboration_plan.py", "--render", "--verify-prepared"], cwd=root, check=True)
