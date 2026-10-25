"""Record this repair without accepting it or changing published baseline evidence."""
import json
from pathlib import Path

root = Path(__file__).resolve().parents[2]
path = root / "docs/协作开发/workboard.json"
board = json.loads(path.read_text(encoding="utf-8"))
commit = "60e58e3dac99d310dec0fb113017ec77ee709a86"
report = "docs/协作开发/验收/2026-10-26-第六批通知恢复接续复核.md"
evidence = ".planning/2026-10-25-developed-review-6/validation.json"
card = next(row for row in board["dispatches"] if row["id"] == "ALN-12-V")
assert card["owner"] == "dsh-lead" and card["status"] == "review"
card["reviewer"] = None
card["next_step"] = (
    "codex-runtime 接续 Claude 复核，发现并修复 3283819 的两项异常窗口(60e58e3)："
    "前态缺失不建空基线，基线读取失败不覆盖待补发标记；固定提交客户端372/372、tsc0。"
    "新增修复待另一审阅人复核；真机通知端到端仍需授权空闲窗口与有已验证互联网的设备，不关闭整卡。"
)
entry = dict(
    date="2026-10-26",
    scope="接续通知恢复复核：快照读取失败与基线读取失败两项缺陷修复；真实组装层回归、投递拒绝与标记写失败边界",
    source_commit=commit, functional_commit=commit,
    source_tree="6eaf50a0b14506d24f34a85eb2861af85546d944",
    result="固定Git归档客户端372 passed/0 failed，tsc退出码0；本轮新临时目录与junction已清理。修复者codex-runtime不自批；未操作真机，端到端缺口保留",
    log=evidence, delivery=report,
)
if not any(row.get("source_commit") == commit for row in card["evidence"]):
    card["evidence"].append(entry)
history = dict(
    date="2026-10-26", **{"from": "review(claude-main 停止，dsh-lead 已收尾3283819)", "to": "review"},
    reason="用户指派 codex-runtime 接续复核；新增60e58e3修复并验证，原owner/交付历史保留，修复待独立复核与真机验收",
)
if history not in card["ownership_history"]:
    card["ownership_history"].append(history)
review_id = "REVIEW-2026-10-26-DEVELOPED-6"
if not any(row["id"] == review_id for row in board["code_review_batches"]):
    board["code_review_batches"].append(dict(
        id=review_id, date="2026-10-26", reviewer="codex-runtime", cards=["ALN-12-V"],
        fixed_cards=["ALN-12-V"], fix_commit=commit, report=report, evidence=evidence,
        status="fixes_delivered_pending_independent_recheck",
        product_acceptance_added=False, published_by_review=False,
    ))
change_id = "discovered-60e58e3dac99"
if not any(row["id"] == change_id for row in board["post_candidate_changes"]):
    board["post_candidate_changes"].append(dict(
        id=change_id, title="fix(ALN-12-V): 保留通知基线读取失败时的待补发状态",
        workpackage="ALN-12", source_commit=commit,
        status="source_registered_pending_validation",
        files=["android-app/src/device/notifications.ts", "android-app/src/offline/backgroundRunner.ts",
               "android-app/test/device/notificationRecovery.test.ts",
               "android-app/test/offline/backgroundNotificationReview.test.ts"],
        candidate_included=False, independent_review=False, report=report,
        scope="固定提交客户端372/372与tsc0；尚未独立接受，未纳入已发布基线，真机通知端到端未完成",
    ))
board["updated_at"] = "2026-10-26"
baseline = board["baseline"]
previous = baseline.get("latest_incremental_verification")
history = baseline.setdefault("incremental_verification_history", [])
if previous and previous.get("source_commit") != commit and previous not in history:
    history.append(previous)
baseline["latest_incremental_verification"] = dict(
    source_commit=commit, source_tree="6eaf50a0b14506d24f34a85eb2861af85546d944",
    client_passed=372, typecheck="passed", device_verified=False, evidence=evidence,
    scope="固定Git归档客户端372/372与TypeScript5.9.3通过；本轮仅客户端，未重跑Python或Android原生构建，未操作真机；不替换已发布基线",
)
path.write_text(json.dumps(board, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
