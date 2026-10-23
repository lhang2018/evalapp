"""Record review results without accepting our own repairs or changing release state."""
from collections import Counter
import importlib.util
import json
from pathlib import Path

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
board_path = root / "docs/协作开发/workboard.json"
board = json.loads(board_path.read_text(encoding="utf-8"))
record = json.loads((folder / "committed-evidence.json").read_text(encoding="utf-8"))
assert all(r["exit_code"] == 0 for r in record["results"])
commit = record["source_commit"]
report = "docs/协作开发/验收/2026-10-24-新增三卡独立审核.md"
ids = {"ALN-07-T": ["CR24-05", "CR24-06"], "ALN-07-R": ["CR24-07"],
       "ALN-16-X": ["CR24-01", "CR24-02", "CR24-03", "CR24-04"]}
for card in board["dispatches"]:
    if card["id"] not in ids:
        continue
    card.update(status="review", reviewer="codex-runtime", review_result="changes_requested",
                review_report=report, peer_findings=ids[card["id"]], fix_author="codex-runtime",
                fix_commit=commit, fix_status="repaired_pending_independent_review",
                review_note="原claude-main交付经独立复核发现问题，codex-runtime修复；本轮修复须其他审阅者复核，不自批",
                next_step="独立复核f633130、27项探针的修复前后判别力与真实SQLite路径；运行时/API/真实环境消费仍按父包另验")
    card["evidence"].append({"date": "2026-10-24", "reviewed_commit": "dfcfceede913b8c85d5333a6b1c6948f75653488",
        "source_commit": commit, "scope": "新增三卡独立审阅及修复，不自批修复",
        "result": "独立27探针原源码26 failed/1 passed；修复后相关155 passed，固定全量1521 passed/1 skipped；客户端同tree复用166/typecheck",
        "report": report, "log": ".planning/2026-10-24-code-review/committed-evidence.json"})

remaining = {
    "ALN-00": "00-V已接受；最新f633130固定源码1521/1及同tree客户端166/typecheck为源码证据。00-B仍需选定范围独立接受、04-UI真机及干净候选发布，不替换历史prepared_candidate",
    "ALN-03": "03-S既定切片及签名key_id修复已获第三方接受；生产信任材料和元数据/制品字节完整联验另验，整包未关闭",
    "ALN-07": "07-W已接受；07-P修订不变量获部分复核、其余恢复切片待复核；07-T/07-R已独立审查并修复f633130，待另一审阅者接受修复。运行时实际消费另验，整包未关闭",
    "ALN-08": "08-M/08-R既定切片及计量修复已接受；工作流/执行模式主链路由、生产评测集、真实价格及未知费用后续对账另验，整包未关闭",
    "ALN-09": "09-F五项修复已独立接受；真实候选资产隔离宿主、持久问题队列及多进程故障恢复仍待验，整包未关闭",
    "ALN-10": "10-Q/R/H均已独立接受；后续消费真实候选隔离宿主与制品版本并证明实际收益，整包未关闭",
    "ALN-14": "14-A由claude-main独立复核接受（0c501bf）；生产候选收益、周期守护、用户前端和规模负载仍另验，整包未关闭",
    "ALN-16": "16-A/16-R已接受；16-X真实存储计数、范围/持久审计、导出防覆盖已由f633130修复，待其他审阅者复核；API/业务仓储、端侧EvidenceEvent及生产凭证另验，整包未关闭",
    "ALN-18": "18-A～F既定切片及覆盖修复均获第三方接受；App/编程专家实际消费、跨文件语义/LSP、包生命周期及真实环境另验，整包未关闭",
}
for package in board["workpackages"]:
    if package["id"] in remaining:
        package["next_step"] = remaining[package["id"]]
verification = dict(source_commit=commit, source_tree=record["source_tree"], python_passed=1521,
    python_skipped=1, client_passed=166, typecheck="passed",
    scope="新增三卡独立审查7组修复；固定git archive排除通知/旧日志在途；27探针及原作者回归全绿；客户端同tree复用8c479b6，不重跑真机；不发布基线",
    evidence="../../.planning/2026-10-24-code-review/committed-evidence.json")
board["baseline"]["latest_source_verification"] = verification
board["updated_at"] = "2026-10-24"
board["peer_review_batch"]["snapshot_semantics"] = "2026-10-23原审阅快照，非当前接受汇总；第三方修复接受见各card字段，2026-10-24新批次另列"
board["peer_review_2026_10_24"] = {"reviewer": "codex-runtime", "cards": list(ids),
    "reviewed_commit": "dfcfceede913b8c85d5333a6b1c6948f75653488", "fix_commit": commit,
    "result": "changes_requested; repaired_pending_independent_review", "report": report}
board_path.write_text(json.dumps(board, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
spec = importlib.util.spec_from_file_location("board_checker", root / "tools/check_collaboration_plan.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)
checker.RENDERED.write_text(checker.render(board), encoding="utf-8", newline="\n")
assert not checker.validate(board), checker.validate(board)
(folder / "inventory.json").write_text(json.dumps({
    "source_commit": commit, "counts": dict(Counter(c["status"] for c in board["dispatches"])),
    "cards": [{k: c.get(k) for k in ("id", "status", "owner", "reviewer", "review_result", "fix_status", "next_step")}
              for c in board["dispatches"]], "published_commit": board["baseline"]["published_commit"],
    "prepared_candidate": board["baseline"]["prepared_candidate"]["commit"]}, ensure_ascii=False, indent=2),
    encoding="utf-8", newline="\n")

for card_id, delivery_name, task_name in (
    ("ALN-07-T", "ALN-07-T-轨迹编译.md", "07-T轨迹编译.md"),
    ("ALN-07-R", "ALN-07-R-可执行恢复策略.md", "07-R可执行恢复策略.md"),
    ("ALN-16-X", "ALN-16-X-导出下载与删除执行.md", "16-X导出下载与删除执行.md"),
):
    path = root / "docs/协作开发/交付" / delivery_name
    text = path.read_text(encoding="utf-8")
    first, rest = text.split("\n", 1)
    text = first + "\n\n2026-10-24独立审核：**需修改，修复后待另一审阅者复核**。codex-runtime发现问题并提交修复`f633130`；27项独立探针原源码26失败/1通过，修复后相关155通过，固定源码1521通过/1跳过。见[独立审核记录](../验收/2026-10-24-新增三卡独立审核.md)。以下为dfcfcee原交付范围和历史证据，不代表修复后的接受结论。\n" + rest
    text = text.replace("## 验证证据", "## 原交付验证证据（历史）")
    text = text.replace("审阅结论：待审", "审阅结论：changes_requested；f633130修复待其他审阅者复核")
    path.write_text(text, encoding="utf-8", newline="\n")
    path = root / "docs/协作开发/任务卡" / task_name
    text = path.read_text(encoding="utf-8").replace("状态：in_progress（2026-10-23 认领）", "状态：review（原交付dfcfcee；2026-10-24审核修复f633130待独立复核）")
    text = text.replace("07-P 计划修订已 accepted 复核范围、07-W 工作流匹配已 accepted", "07-P 计划修订不变量已部分复核、整卡仍review；07-W 工作流匹配已accepted")
    path.write_text(text, encoding="utf-8", newline="\n")
