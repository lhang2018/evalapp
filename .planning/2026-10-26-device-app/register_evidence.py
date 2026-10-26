"""Record the bounded device-task delivery without claiming product acceptance."""
import hashlib
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
prefix = folder.relative_to(root).as_posix()
commit = "c2e6871d1bc3c0011a43788d3dc2f0089dc76fbc"
tree = "4b9e729c5802a30c6d2db7c6a6635abe07d44afa"
report = "docs/协作开发/验收/2026-10-26-12E本机任务App挂载.md"
evidence = prefix + "/committed-evidence.json"
client = json.loads((folder / "validation.json").read_text(encoding="utf-8"))
backend = json.loads((folder / "backend-validation.json").read_text(encoding="utf-8"))
assert client["commit"] == backend["snapshot_commit"] == commit
assert client["source_tree"] == backend["source_tree"] == tree
assert not any(client["results"].values()) and backend["exit_code"] == 0
assert client["temporary_data_removed"] and backend["temporary_data_removed"]
summary = dict(source_commit=commit, source_tree=tree, client_passed=405, typecheck="passed",
    backend_related_passed=13, backend_full_rerun=False, device_verified=False,
    scope="本机打开设置纵切片已挂载App；非真机/非任意多步骤或服务端设备规划验收",
    new_temporary_data_removed=True,
    evidence_sha256_lf={name: hashlib.sha256((folder / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        for name in ["validation.json", "backend-validation.json", "initial-tests.txt", "boundary-probes.txt",
                     "focused-tests.txt", "client-tests.txt", "typecheck.txt", "backend-tests.txt"]})
(folder / "committed-evidence.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
path = root / "docs/协作开发/workboard.json"
board = json.loads(path.read_text(encoding="utf-8"))
card = next(row for row in board["dispatches"] if row["id"] == "ALN-12-E")
assert card["status"] == "review"
card["next_step"] = "c2e6871已挂载本机打开设置纵切片：明确确认→持久执行→回执→用户验收；排除服务端重提，固定客户端405/405+tsc0、事件同步13通过。新增实现待复核与真机APK/JS绑定验收；任意多步骤/服务端设备规划与人工裁决仍未完成。"
entry = dict(date="2026-10-26", source_commit=commit, functional_commit=commit, source_tree=tree,
    scope="12-E本机任务App挂载，含取消/中断/并发/投影与元数据重传边界；同KV多仓库串行与回执版本去重",
    result="固定客户端405/405、tsc0、后端事件同步13通过；App经类型检查与编排集成测试，未跑RN UI自动化/未构建APK/未使用真机；原作者与review状态保留，未发布",
    log=evidence, delivery=report)
if not any(isinstance(row, dict) and row.get("source_commit") == commit for row in card["evidence"]):
    card["evidence"].append(entry)
    card.setdefault("ownership_history", []).append(dict(date="2026-10-26",
        **{"from": "review(原作者zcode-main，提议与回执模块)", "to": "review"},
        reason="用户指示codex-runtime接续App挂载；c2e6871交付本机打开设置纵切片，保留原owner/reviewer，新增实现待复核与真机验收"))
parent = next(row for row in board["workpackages"] if row["id"] == "ALN-12")
parent["next_step"] = "12-A协议已接受；12-E本机打开设置App纵切片已交付review(c2e6871)，通用服务端设备任务/多步骤/人工裁决另做；12-V联网真机通知/离线恢复与发布包验收仍待完成，父包不关闭。"
notification = next(row for row in board["dispatches"] if row["id"] == "ALN-12-V")
if not any(isinstance(row, dict) and row.get("source_commit") == commit for row in notification["evidence"]):
    notification["evidence"].append(dict(entry, scope="关联12-E：本机取消终态正确投影为取消通知；客户端405项包含原通知回归。本轮未执行WorkManager或真实通知投递"))
change_id = "discovered-c2e6871d1bc3"
if not any(row["id"] == change_id for row in board["post_candidate_changes"]):
    files = subprocess.check_output(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit], cwd=root, text=True).splitlines()
    board["post_candidate_changes"].append(dict(id=change_id, title="feat(ALN-12-E): 挂载本机设备任务确认执行与持久回执",
        workpackage="ALN-12", source_commit=commit, status="source_registered_pending_validation",
        files=files, candidate_included=False, independent_review=False, report=report,
        scope="固定客户端405/405、tsc0与事件同步13；未进行真机/产品验收，未纳入已发布基线"))
batch_id = "INTEGRATION-2026-10-26-DEVICE-APP"
if not any(row["id"] == batch_id for row in board["code_review_batches"]):
    board["code_review_batches"].append(dict(id=batch_id, date="2026-10-26", author="codex-runtime",
        cards=["ALN-12-E"], fix_commit=commit, report=report, evidence=evidence,
        status="implementation_delivered_pending_review", product_acceptance_added=False, published_by_review=False))
baseline = board["baseline"]
previous = baseline.get("latest_incremental_verification")
history = baseline.setdefault("incremental_verification_history", [])
if previous and previous.get("source_commit") != commit and previous not in history:
    history.append(previous)
baseline["latest_incremental_verification"] = dict(source_commit=commit, source_tree=tree,
    client_passed=405, typecheck="passed", device_verified=False, evidence=evidence,
    scope="固定Git归档客户端405/405与TypeScript5.9.3、后端事件同步相关13通过；App本机纵切片已接线，未跑RN UI自动化/未构建APK/未使用真机，非后端全量；不替换发布基线")
board["updated_at"] = "2026-10-26"
path.write_text(json.dumps(board, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
