"""Register a bounded main-chain repair and its exact-commit evidence."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
commit = "077d2939236dff71821ab69157ea50bf4d1dbbeb"
tree = "caad45c5895bcd9d7f373ece7017b682ed69bbf3"
report = "docs/协作开发/验收/2026-10-26-07C与12E主链接续复核.md"
prefix = folder.relative_to(root).as_posix()
evidence = prefix + "/committed-evidence.json"
records = {name: json.loads((folder / name).read_text(encoding="utf-8"))
           for name in ["validation.json", "backend-validation.json"]}
assert records["validation.json"]["commit"] == commit
assert records["backend-validation.json"]["snapshot_commit"] == commit
assert all(row["source_tree"] == tree and row["temporary_data_removed"] for row in records.values())
assert not any(records["validation.json"]["results"].values())
assert records["backend-validation.json"]["exit_code"] == 0
summary = dict(
    source_commit=commit, source_tree=tree, client_passed=384, typecheck="passed",
    backend_related_passed=112, backend_full_rerun=False, device_verified=False,
    scope="07-C只读再检；12-E设备确认/原生状态/安全停止修复，非App挂载或真机验收",
    records={name: prefix + "/" + name for name in records},
    evidence_sha256_lf={name: hashlib.sha256((folder / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
                       for name in ["validation.json", "backend-validation.json", "negative-probes.txt",
                                    "focused-tests.txt", "client-tests.txt", "typecheck.txt", "backend-tests.txt"]},
    new_temporary_data_removed=True,
)
(folder / "committed-evidence.json").write_text(
    json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
path = root / "docs/协作开发/workboard.json"
board = json.loads(path.read_text(encoding="utf-8"))
for task_id, next_step, scope in [
    ("ALN-07-C", "固定077d293只读再检：绑定/对账/授权相关112项通过，未见新阻断；保留原审阅人与review，含本会话历史修复不自批；人工裁决与整包缺口另验。", "07-C只读再检：实际ReAct意图登记回填、controller恢复对账消费、原子回滚与授权边界"),
    ("ALN-12-E", "077d293修复确认回调改写动作、非布尔真值误授权/误报成功、异步等待越过安全停止；固定客户端384/384+tsc0。新增修复待其他审阅人复核；App挂载、完整任务生命周期与真机动作仍未验收。", "12-E执行闸门修复；12项负向探针修前全失败，修后相关72项通过；不代表App端到端完成"),
]:
    card = next(row for row in board["dispatches"] if row["id"] == task_id)
    assert card["status"] == "review"
    card["next_step"] = next_step
    if not any(isinstance(row, dict) and row.get("log") == evidence for row in card["evidence"]):
        card["evidence"].append(dict(
            date="2026-10-26", source_commit=commit, source_tree=tree,
            scope=scope, result="客户端384/384与tsc0；后端相关112通过，非后端全量；未使用真机，未发布",
            log=evidence, delivery=report,
        ))
change_id = "discovered-077d2939236d"
if not any(row["id"] == change_id for row in board["post_candidate_changes"]):
    board["post_candidate_changes"].append(dict(
        id=change_id, title="fix(ALN-12-E): 固定设备确认动作并阻止异步跨越安全停止",
        workpackage="ALN-12", source_commit=commit, status="source_registered_pending_validation",
        files=["android-app/src/device/broker.ts", "android-app/src/device/nativeTransport.ts",
               "android-app/test/device/mainchainReview.test.ts"],
        candidate_included=False, independent_review=False, report=report,
        scope="固定客户端384/384、tsc0与后端相关112；新增修复待独立复核，未纳入已发布基线",
    ))
review_id = "REVIEW-2026-10-26-MAINCHAIN"
if not any(row["id"] == review_id for row in board["code_review_batches"]):
    board["code_review_batches"].append(dict(
        id=review_id, date="2026-10-26", reviewer="codex-runtime",
        cards=["ALN-07-C", "ALN-12-E"], fixed_cards=["ALN-12-E"],
        read_only_recheck_cards=["ALN-07-C"], fix_commit=commit, report=report, evidence=evidence,
        status="fixes_delivered_pending_independent_recheck", product_acceptance_added=False,
        published_by_review=False,
    ))
baseline = board["baseline"]
previous = baseline.get("latest_incremental_verification")
history = baseline.setdefault("incremental_verification_history", [])
if previous and previous.get("source_commit") != commit and previous not in history:
    history.append(previous)
baseline["latest_incremental_verification"] = dict(
    source_commit=commit, source_tree=tree, client_passed=384, typecheck="passed",
    device_verified=False, evidence=evidence,
    scope="固定Git归档客户端384/384、TypeScript5.9.3与后端绑定/对账/授权相关112通过；非后端全量，未构建APK、未使用真机，不改变已发布基线",
)
board["updated_at"] = "2026-10-26"
path.write_text(json.dumps(board, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
