"""Register exact-commit repair evidence; preserve author and acceptance boundaries."""
import hashlib
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
prefix = folder.relative_to(root).as_posix()
commit = "998db6fd6427586a4ba23bd99bf91a7edf03625e"
tree = "8ef3aed41a3fcc77d5bff720e3ca90450d94d713"
report = "docs/协作开发/验收/2026-10-26-12E确认范围与回执复核.md"
evidence = prefix + "/committed-evidence.json"
client = json.loads((folder / "validation.json").read_text(encoding="utf-8"))
backend = json.loads((folder / "backend-validation.json").read_text(encoding="utf-8"))
assert client["commit"] == backend["snapshot_commit"] == commit
assert client["source_tree"] == backend["source_tree"] == tree
assert not any(client["results"].values()) and backend["exit_code"] == 0
assert client["temporary_data_removed"] and backend["temporary_data_removed"]
summary = dict(source_commit=commit, source_tree=tree, client_passed=424, typecheck="passed",
    backend_related_passed=13, backend_full_rerun=False, device_verified=False,
    scope="本机任务确认范围、回执降级/并发验收及台账输入校验；非真机或独立接受",
    new_temporary_data_removed=True,
    evidence_sha256_lf={name: hashlib.sha256((folder / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        for name in ["validation.json", "backend-validation.json", "negative-probes.txt", "focused-tests.txt",
                     "focused-tests-final.txt", "client-tests.txt", "typecheck.txt", "backend-tests.txt"]})
(folder / "committed-evidence.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
path = root / "docs/协作开发/workboard.json"
board = json.loads(path.read_text(encoding="utf-8"))
card = next(row for row in board["dispatches"] if row["id"] == "ALN-12-E")
assert card["status"] == "review"
card["next_step"] = "本机打开设置App纵切片c2e6871已接线；998db6f进一步修确认范围、回执降级/验收竞态、非法恢复回执。固定客户端424/424+tsc0、事件同步13通过。新增修复待复核与真机APK/JS绑定验收；多步骤/服务端设备规划与人工裁决等仍未完成。"
entry = dict(date="2026-10-26", source_commit=commit, functional_commit=commit, source_tree=tree,
    scope="12-E接续复核：原16项负向全失败；修复保存动作偏离确认范围、回执降级/验收窗口、台账输入与恢复身份校验；新增接受版本留痕",
    result="相关68通过；固定客户端424/424、tsc0、事件同步13通过；同JS/KV实例写保护，不是跨进程事务；未构建APK/未使用真机/未独立接受/未发布",
    log=evidence, delivery=report)
if not any(isinstance(row, dict) and row.get("source_commit") == commit for row in card["evidence"]):
    card["evidence"].append(entry)
    card.setdefault("ownership_history", []).append(dict(date="2026-10-26", **{"from": "review", "to": "review"},
        reason="codex-runtime接续自检并修复998db6f；保留原owner/reviewer，新修复待独立复查与真机验收，不改原作者历史"))
change_id = "discovered-998db6fd6427"
if not any(row["id"] == change_id for row in board["post_candidate_changes"]):
    files = subprocess.check_output(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit], cwd=root, text=True).splitlines()
    board["post_candidate_changes"].append(dict(id=change_id, title="fix(ALN-12-E): 限定确认动作并按当前回执保存验收",
        workpackage="ALN-12", source_commit=commit, status="source_registered_pending_validation",
        files=files, candidate_included=False, independent_review=False, report=report,
        scope="固定客户端424/424、tsc0与事件同步13；修复待独立复核、未做真机验收或纳入发布基线"))
batch_id = "REVIEW-2026-10-26-DEVICE-SCOPE"
if not any(row["id"] == batch_id for row in board["code_review_batches"]):
    board["code_review_batches"].append(dict(id=batch_id, date="2026-10-26", reviewer="codex-runtime",
        cards=["ALN-12-E"], fixed_cards=["ALN-12-E"], fix_commit=commit, report=report, evidence=evidence,
        status="fixes_delivered_pending_independent_recheck", product_acceptance_added=False, published_by_review=False))
baseline = board["baseline"]
previous = baseline.get("latest_incremental_verification")
history = baseline.setdefault("incremental_verification_history", [])
if previous and previous.get("source_commit") != commit and previous not in history:
    history.append(previous)
baseline["latest_incremental_verification"] = dict(source_commit=commit, source_tree=tree,
    client_passed=424, typecheck="passed", device_verified=False, evidence=evidence,
    scope="固定Git归档客户端424/424与TypeScript5.9.3、后端事件同步相关13通过；本机任务确认与回执复核，非后端全量/真机/独立接受；不替换发布基线")
board["updated_at"] = "2026-10-26"
path.write_text(json.dumps(board, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
