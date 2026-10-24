"""Register scoped review fixes while preserving third-party acceptances."""
import importlib.util
import json
from pathlib import Path

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
evidence = json.loads((folder / "committed-evidence.json").read_text(encoding="utf-8"))
assert all(r["exit_code"] == 0 for r in evidence["results"])
commit = evidence["source_commit"]
report = "docs/协作开发/验收/2026-10-24-ALN-07-E独立审核.md"
board_path = root / "docs/协作开发/workboard.json"
board = json.loads(board_path.read_text(encoding="utf-8"))
card = next(c for c in board["dispatches"] if c["id"] == "ALN-07-E")
assert card["owner"] == "claude-main" and card["status"] == "review"
card.update(review_result="changes_requested", review_report=report,
    review_note="原交付存在跨任务/重复/歧义归因、可伪造结论和输入/终态口径问题；codex-runtime已修复，须其他审阅者复核，不自批",
    fix_author="codex-runtime", fix_commit=commit, fix_status="repaired_pending_independent_review",
    peer_findings=[f"PR07E-{i:02}" for i in range(1, 7)],
    next_step="独立复核26aaec7、29项探针原源码28失败/1通过与真实SQLite只读路径；新增ambiguous/resolved、显式step_bindings及派生只读报告，不接运行时、不关闭父包")
card["evidence"].append(dict(date="2026-10-24", reviewed_commit="c91621ea79dd190dc3f62955bd8f94c29fd19b3c",
    source_commit=commit, scope="独立审核六组问题及修复；不自批", result="29 new probes; original28 failed/1 passed; related125 passed; committed1566 passed/1 skipped; client166/typecheck reused identical tree",
    report=report, log=".planning/2026-10-24-aln07e-review/committed-evidence.json"))
updates = {
    "ALN-01": "01-A已由claude-main独立接受（字节级清单18/18、12探针三时点及1065/1）；下游仍各自对接，不自动发布共同基线",
    "ALN-05": "05-M及05-I受信身份/主链读写/持久清单既定切片均已第三方接受；Android缓存/隐私仓储与完整IAM另验，整包未关闭",
    "ALN-07": "07-E经独立审核发现六组问题已由26aaec7修复、待另一审阅者接受；07-W/T/R已接受，07-P剩余恢复切片待核验；实际运行时工作流/轨迹/恢复/对账消费另验，整包未关闭",
    "ALN-15": "15-A及15-R四消费链/聚合账户既定切片均获第三方接受；真实账单、旧Goal迁移、provider/expert原子化与设备资源调度另验，整包未关闭",
}
for p in board["workpackages"]:
    if p["id"] in updates:
        p["next_step"] = updates[p["id"]]
board["baseline"]["latest_source_verification"] = dict(source_commit=commit, source_tree=evidence["source_tree"],
    python_passed=1566, python_skipped=1, client_passed=166, typecheck="passed",
    scope="07-E六组独立审核修复、29探针/125相关；固定Git归档排除在途，后端1566/1；客户端同tree复用8c479b6，未重跑真机，不发布基线",
    evidence="../../.planning/2026-10-24-aln07e-review/committed-evidence.json")
board["latest_review_aln07e"] = dict(reviewer="codex-runtime", reviewed_commit="c91621ea79dd190dc3f62955bd8f94c29fd19b3c",
    fix_commit=commit, result="changes_requested; repaired_pending_independent_review", report=report)
board["updated_at"] = "2026-10-24"
board_path.write_text(json.dumps(board, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
spec = importlib.util.spec_from_file_location("checker", root / "tools/check_collaboration_plan.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)
checker.RENDERED.write_text(checker.render(board), encoding="utf-8", newline="\n")
assert not checker.validate(board), checker.validate(board)

path = root / "docs/协作开发/交付/ALN-07-E-副作用账本对账.md"
text = path.read_text(encoding="utf-8")
title, rest = text.split("\n", 1)
text = title + "\n\n最新审阅：**changes_requested，修复26aaec7待另一审阅者复核**。六组问题、29项探针原代码28失败/1通过；修复后相关125通过、固定源码1566通过/1跳过。契约新增任务校验、归因歧义/显式step_bindings、resolved终态及派生不可变报告，见[独立审核](../验收/2026-10-24-ALN-07-E独立审核.md)。下文为c91621e原交付和历史五类契约，当前行为以修复及审核记录为准，不扩大到运行时接线。\n" + rest
path.write_text(text, encoding="utf-8", newline="\n")
path = root / "docs/协作开发/任务卡/07-E副作用账本对账.md"
text = path.read_text(encoding="utf-8")
text = text.replace("负责人 claude-main。", "负责人claude-main；状态review。codex-runtime独立审核发现六组问题，26aaec7修复待其他审阅者复核，见[审核记录](../验收/2026-10-24-ALN-07-E独立审核.md)。")
text += "\n## 审核后契约补记\n\n原五类契约补充ambiguous（已确认但步骤归因不唯一）及resolved（failed/compensated已定论）；同工具多步骤只接受宿主显式step_bindings，不按排序猜测。task_id/状态/记录唯一性必须校验；ok由不可变明细推导，不能由序列化输入指定。旧缺来源报告需从实际账本重生成。宿主动作授权、真实关联、参数/版本证明和实际运行时消费仍另验。\n"
path.write_text(text, encoding="utf-8", newline="\n")
