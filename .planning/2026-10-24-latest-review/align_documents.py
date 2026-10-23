"""Clarify latest review evidence and propagate existing acceptance, never self-accept."""
import importlib.util
import json
from pathlib import Path

root = Path(__file__).resolve().parents[2]
board_path = root / "docs/协作开发/workboard.json"
board = json.loads(board_path.read_text(encoding="utf-8"))
for card in board["dispatches"]:
    if card["id"] in {"ALN-07-T", "ALN-07-R", "ALN-16-X"}:
        assert card["status"] == "accepted" and card["third_party_review"]["reviewer"] == "claude-main"
        card["next_step"] = "claude-main已于3820e5d独立接受f633130；X组8探针两时点0/8→8/8，独立覆盖六组发现部分行为，其余含快照/持久化由27项回归覆盖；原测试34项、联合100项。运行时/API及真实环境按父包另验，整包不关闭"
        card["review_note"] = "原审核发现问题并由codex-runtime修复；f633130现已由claude-main独立接受。原changes_requested是历史记录，独立探针覆盖边界见第三方报告及最新提交复核"
        card["third_party_review"]["result"] = "X组8探针两时点：dfcfcee 0/8、f633130 8/8，直接覆盖CR24-01/02/03/04/05/07部分行为；CR24-06及其他子边界由被审方27项回归覆盖。原作者测试34（13+11+10）项未改；联合100通过；固定全量1521/1；本轮在6a4c0b5归档再次8/8、100通过"
        card["third_party_review"]["scope_note"] = "无CR24-06单独第三方探针，不声称X组覆盖全部七组所有边界；既有接受结论保留"
for package in board["workpackages"]:
    if package["id"] == "ALN-07":
        package["next_step"] = "07-W及07-T/07-R的f633130修复已独立接受；07-P修订不变量获部分复核、其余恢复切片待复核；运行时实际工作流/轨迹/恢复消费和副作用对账另验，整包未关闭"
    if package["id"] == "ALN-16":
        package["next_step"] = "16-A/16-R及16-X的f633130修复已独立接受；API/业务仓储、端侧EvidenceEvent及生产凭证仍另验，不宣称全域擦除、跨层原子删除或整包关闭"
board["peer_review_2026_10_24"]["snapshot_semantics"] = "原审核时点，三卡后续由claude-main于3820e5d接受；当前状态以dispatches及third_party_review为准"
board["latest_review_evidence_check"] = {"reviewed_commit": "6a4c0b570a3955fbe7558b28541ddb441c2e0d39",
    "reviewer": "codex-runtime", "report": "docs/协作开发/验收/2026-10-24-最新提交复核.md",
    "result": "业务源码未变；探针0/8→8/8，联合100通过；纠正覆盖/计数/历史状态，保留已接受结果"}
board_path.write_text(json.dumps(board, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
spec = importlib.util.spec_from_file_location("board_checker", root / "tools/check_collaboration_plan.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)
checker.RENDERED.write_text(checker.render(board), encoding="utf-8", newline="\n")
assert not checker.validate(board), checker.validate(board)

for delivery, task in (("ALN-07-T-轨迹编译.md", "07-T轨迹编译.md"),
                       ("ALN-07-R-可执行恢复策略.md", "07-R可执行恢复策略.md"),
                       ("ALN-16-X-导出下载与删除执行.md", "16-X导出下载与删除执行.md")):
    path = root / "docs/协作开发/交付" / delivery
    text = path.read_text(encoding="utf-8")
    first, paragraph, rest = text.split("\n\n", 2)
    text = first + "\n\n最新结论：**f633130修复已由claude-main于3820e5d独立接受**，范围见[第三方复核](../验收/2026-10-24-三卡修复第三方复核.md)。[原审核记录](../验收/2026-10-24-新增三卡独立审核.md)中的changes_requested保留历史；以下dfcfcee原交付范围和验证证据不替代最新接受结论。运行时/API与真实环境仍按父包另验。\n\n" + rest
    text = text.replace("审阅人：待定（须作者之外独立复核）", "审阅人：codex-runtime（原交付）；claude-main（f633130修复独立复核）")
    text = text.replace("认领状态：review（待独立复核）", "认领状态：accepted（3820e5d独立接受，整包未关闭）")
    text = text.replace("审阅结论：changes_requested；f633130修复待其他审阅者复核", "审阅结论：accepted；f633130修复由claude-main独立接受，原问题记录保留历史")
    path.write_text(text, encoding="utf-8", newline="\n")
    path = root / "docs/协作开发/任务卡" / task
    text = path.read_text(encoding="utf-8").replace("状态：review（原交付dfcfcee；2026-10-24审核修复f633130待独立复核）",
                                                 "状态：accepted（3820e5d：claude-main独立接受f633130修复，整包未关闭）")
    path.write_text(text, encoding="utf-8", newline="\n")
