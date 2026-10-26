"""Record partitioned fixed-tree validation without claiming production acceptance."""
import hashlib
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
prefix = folder.relative_to(root).as_posix()
commit = "5f09fb845c1bae952cd81863b4b149bf7389bdc5"
client_commit = "998db6fd6427586a4ba23bd99bf91a7edf03625e"
report = "docs/协作开发/验收/2026-10-26-主动规划与团队标识接续复核.md"
evidence = prefix + "/committed-evidence.json"
records = {name: json.loads((folder / name).read_text(encoding="utf-8")) for name in [
    "negative-validation.json", "negative-corrected-validation.json", "focused-validation.json",
    "focused-final-validation.json", "fixed-validation.json", "tools-validation.json"]}
fixed, tooling = records["fixed-validation.json"], records["tools-validation.json"]
assert fixed["snapshot_commit"] == tooling["snapshot_commit"] == commit
assert fixed["source_tree"] == tooling["source_tree"]
assert fixed["exit_code"] == tooling["exit_code"] == 0
assert all(row["temporary_data_removed"] for row in records.values())
def git(*args):
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()
client_tree = git("rev-parse", commit + ":android-app")
assert client_tree == git("rev-parse", client_commit + ":android-app")
assert "2245 passed, 1 skipped, 1 deselected" in (folder / "full-backend.txt").read_text(encoding="utf-8")
assert "70 passed" in (folder / "tools-tests.txt").read_text(encoding="utf-8")
logs = ["negative-probes.txt", "negative-corrected.txt", "focused-tests.txt", "focused-final.txt", "full-backend.txt", "tools-tests.txt"]
summary = dict(source_commit=commit, source_tree=fixed["source_tree"], python_passed=2315,
    python_skipped=1, python_deselected=1, validation_scope="tests + tools/tests, same Git object, two invocations",
    partitions={"tests": 2245, "tools/tests": 70}, records=records,
    client_passed=424, typecheck="passed", client_evidence_mode="reused_identical_client_tree",
    client_source_commit=client_commit, client_tree=client_tree,
    client_evidence=".planning/2026-10-26-device-review/committed-evidence.json",
    device_verified=False, product_acceptance_added=False, new_temporary_data_removed=True,
    evidence_sha256_lf={name: hashlib.sha256((folder / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest() for name in logs})
(folder / "committed-evidence.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
path = root / "docs/协作开发/workboard.json"
board = json.loads(path.read_text(encoding="utf-8"))
next_steps = {
    "ALN-25-B": "5f09fb8修复实际资源采样无效被主动层当低压放行；内置资源层原前台策略保留。相关109、固定完整后端2315/1/1通过；新增修复待复核，主链装配/真实资源非干扰仍未完成。",
    "ALN-26-A": "5f09fb8修复domain/intent分隔符混合缺口证据；真实候选登记与重复去重通过。固定完整后端2315/1/1；新修复待复核，线上遥测/API/UI/真实预算保留。",
    "ALN-26-B": "5f09fb8默认ID纳入任务、角色与专家版本并规范化编码，角色版本快照冻结；显式ID不重写。固定完整后端2315/1/1；新修复待复核，真实运行装配/权限预算仍未完成。",
    "ALN-27-B": "固定5f09fb8只读再检：组合/闭包/仓储与版本绑定回归未见新阻断；不自批6ae4b30。本轮未改组合代码，生产执行、动态展开与独立验收仍保留。",
}
for card in board["dispatches"]:
    if card["id"] not in next_steps:
        continue
    assert card["status"] == "review"
    card["next_step"] = next_steps[card["id"]]
    if not any(isinstance(row, dict) and row.get("log") == evidence for row in card["evidence"]):
        card["evidence"].append(dict(date="2026-10-26", source_commit=commit, source_tree=fixed["source_tree"],
            scope="27-B只读再检、无新修复" if card["id"] == "ALN-27-B" else "主动采样/缺口/团队标识接续复核修复，按本卡范围",
            result="相关109；固定Git两分区合计2315 passed/1 skipped/1 deselected；客户端同子树沿用424非重跑；未做生产/真机或独立接受，未发布",
            log=evidence, delivery=report))
change_id = "discovered-5f09fb845c1b"
if not any(row["id"] == change_id for row in board["post_candidate_changes"]):
    board["post_candidate_changes"].append(dict(id=change_id, title="fix(review): 隔离主动资源未知与规划身份碰撞",
        workpackage="ALN-26", source_commit=commit, status="source_registered_pending_validation",
        files=git("diff-tree", "--no-commit-id", "--name-only", "-r", commit).splitlines(),
        candidate_included=False, independent_review=False, report=report,
        scope="25-B/26-A/26-B修复；27-B只读；完整后端2315/1/1，新修复待独立接受且未纳入基线"))
batch_id = "REVIEW-2026-10-26-PROACTIVE"
if not any(row["id"] == batch_id for row in board["code_review_batches"]):
    board["code_review_batches"].append(dict(id=batch_id, date="2026-10-26", reviewer="codex-runtime",
        cards=list(next_steps), fixed_cards=["ALN-25-B", "ALN-26-A", "ALN-26-B"], read_only_recheck_cards=["ALN-27-B"],
        fix_commit=commit, report=report, evidence=evidence, status="fixes_delivered_pending_independent_recheck",
        product_acceptance_added=False, published_by_review=False))
baseline = board["baseline"]
previous = baseline.get("latest_source_verification")
history = baseline.setdefault("source_verification_history", [])
if previous and previous.get("source_commit") != commit and previous not in history:
    history.append(previous)
baseline["latest_source_verification"] = dict(source_commit=commit, source_tree=fixed["source_tree"],
    python_passed=2315, python_skipped=1, python_deselected=1, client_passed=424, typecheck="passed",
    client_source_commit=client_commit, client_evidence_mode="reused_identical_client_tree", device_verified=False,
    scope="精确Git归档tests2245+tools/tests70合计2315/1/1；客户端子树相同沿用998db6f的424与类型检查，非重跑；无真机/生产接受，不替换已发布基线",
    evidence="../../" + evidence)
board["updated_at"] = "2026-10-26"
path.write_text(json.dumps(board, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
