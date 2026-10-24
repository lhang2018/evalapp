"""Preserve the concurrent third-party ALN-07-E acceptance and align summaries."""
import importlib.util
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2]
assert not subprocess.check_output(["git", "diff", "aafca22", "HEAD", "--", "evoagent", "tests", "android-app"], cwd=root).strip()
path = root / "docs/协作开发/workboard.json"
board = json.loads(path.read_text(encoding="utf-8"))
card = next(c for c in board["dispatches"] if c["id"] == "ALN-07-E")
assert card["status"] == "accepted" and card["third_party_review"]["reviewer"] == "claude-main"
assert card["fix_author"] == "codex-runtime" and card["fix_status"] == "fix_verified_by_third_party"
card["review_note"] = "原六组问题已由codex-runtime修复26aaec7，claude-main在e215d23独立复核接受既定切片；原审核报告保留问题来源，最新结论见third_party_review，不扩大到运行时或父包"
for package in board["workpackages"]:
    if package["id"] == "ALN-07":
        package["next_step"] = "07-E修复26aaec7已由claude-main第三方复核接受；07-W/T/R及P原planner不变量部分接受保持；P运行时aafca22累计修复待其他审阅者复核；精确动作绑定与工作流/轨迹/自动恢复/对账实际消费另验，整包未关闭"
path.write_text(json.dumps(board, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
spec = importlib.util.spec_from_file_location("checker", root / "tools/check_collaboration_plan.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)
checker.RENDERED.write_text(checker.render(board), encoding="utf-8", newline="\n")
assert not checker.validate(board), checker.validate(board)

def replace(relative, pairs):
    p = root / relative
    text = p.read_text(encoding="utf-8")
    for old, new in pairs:
        assert old in text, (relative, old)
        text = text.replace(old, new)
    p.write_text(text, encoding="utf-8", newline="\n")

replace("docs/协作开发/README.md", [
    ("31 accepted、4 review", "32 accepted、3 review"),
    ("新增07-E经本轮审核发现六组问题，`26aaec7`修复待其他审阅者复核，见[07-E独立审核](验收/2026-10-24-ALN-07-E独立审核.md)。", "07-E六组修复`26aaec7`已由claude-main第三方复核接受，见[独立复核](验收/2026-10-24-ALN-07-E修复第三方复核.md)。"),
    ("07-E及07-P运行时修复仍待独立接受。", "07-E已接受，07-P运行时修复仍待独立接受。"),
    ("07-E及07-P运行时本轮修复、13-A及04-UI继续单列。", "07-E修复亦已第三方接受；07-P运行时本轮修复、13-A及04-UI继续单列。"),
])
replace("docs/项目现状与审阅进展_2026-10-24.md", [
    ("31 accepted、4 review", "32 accepted、3 review"),
    ("31张接受是", "32张接受是"),
    ("新增07-E原交付经独立审核发现六组问题，修复26aaec7待其他审阅者复核，[审核记录](协作开发/验收/2026-10-24-ALN-07-E独立审核.md)。", "07-E原交付六组问题的26aaec7修复已由claude-main第三方复核接受，[独立复核](协作开发/验收/2026-10-24-ALN-07-E修复第三方复核.md)。"),
    ("07-W/T/R接受；07-P原planner切片复核、本轮运行时修复待复核；07-E修复待复核 | P/E新修复独立复核", "07-W/T/R/E接受；07-P原planner切片复核、本轮运行时修复待复核 | P运行时新修复独立复核"),
    ("1. 独立复核07-P运行时`aafca22`及07-E修复`26aaec7`，重点是任务/唯一性、歧义归因、派生报告和原生终态语义；不自批本轮修复。", "1. 独立复核07-P运行时`aafca22`，重点是依赖门控、坏计划拒绝、失败归因、恢复上下文及权限拒绝传输；07-E修复已获独立接受，不重复列待审。"),
    ("2. 待审为07-E、07-P、13-A及04-UI；", "2. 待审为07-P、13-A及04-UI；"),
])
replace("docs/项目现状与设计差距核对_2026-10-23.md", [
    ("40张子卡、31 accepted", "40张子卡、32 accepted"),
    ("07-E及07-P运行时审核修复待复核", "07-E修复已独立接受、07-P运行时修复待复核"),
])
replace("docs/协作开发/验收/2026-10-24-ALN-07-P运行时补审.md", [
    ("04-UI四路径真机、13-A、07-E等独立审阅", "04-UI四路径真机、13-A与07-P运行时独立审阅"),
    ("31 accepted/4 review", "32 accepted/3 review"),
])
replace("docs/协作开发/交付/ALN-07-E-副作用账本对账.md", [
    ("最新审阅：**changes_requested，修复26aaec7待另一审阅者复核**。", "最新审阅：**26aaec7修复已由claude-main第三方复核接受（e215d23，本卡切片）**，见[独立复核](../验收/2026-10-24-ALN-07-E修复第三方复核.md)。"),
])
replace("docs/协作开发/任务卡/07-E副作用账本对账.md", [
    ("负责人claude-main；状态review。codex-runtime独立审核发现六组问题，26aaec7修复待其他审阅者复核，见[审核记录](../验收/2026-10-24-ALN-07-E独立审核.md)。", "负责人claude-main；状态accepted（既定纯读切片）。codex-runtime发现六组问题并修复26aaec7，claude-main已第三方复核接受，见[独立复核](../验收/2026-10-24-ALN-07-E修复第三方复核.md)；实际运行时消费另验。"),
])
with (root / ".planning/2026-10-24-plan-runtime-review/progress.md").open("a", encoding="utf-8", newline="\n") as output:
    output.write("\n- 提交前HEAD守卫检测到e215d23/b69e9aa同期文档提交，未暂存/覆盖他人成果；后续校验因本轮证据尚未提交无法读取属预期顺序问题。核对新提交无业务源码变化，保留07-E第三方接受和已纳入该提交的P登记，同步32 accepted/3 review与P运行时待复核；补齐本轮证据并按新HEAD提交。\n")
