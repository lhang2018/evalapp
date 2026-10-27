"""Bind five follow-up reviews to a fixed source and an own-field metadata delta."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
source = "1015f543923b5ac50e106cbf23a7331892965d70"
report = "docs/协作开发/验收/2026-10-27-其余五项与基准证据复核.md"
evidence = ".planning/2026-10-27-other-review/committed-evidence.json"
records = {name: json.loads((folder / (name + ".json")).read_text(encoding="utf-8"))
           for name in ("negative-validation", "focused-validation", "extra-validation", "final-validation")}
for record in records.values():
    owned = Path(record["owned_temp_directory"])
    assert record["temporary_data_removed"] and not owned.exists()
    assert owned.resolve().parent == (root / "outputs").resolve()
assert records["final-validation"]["snapshot_commit"] == source and records["final-validation"]["exit_code"] == 0
assert "278 passed" in (folder / "final-tests.txt").read_text(encoding="utf-8")
scopes = {
    "ALN-26-A": "权威任务/目标证据、快照新鲜性比对、去重与既有演化候选登记；本轮无新阻断",
    "ALN-27-A": "知识验证/撤销/持久化和同专家同逻辑任务质量对照；本轮无新阻断",
    "ALN-27-B": "精确依赖闭包/摘要、可消费状态与IO兼容、组合草稿及版本固定；本轮无新阻断",
    "ALN-09-H": "真实候选资产/子进程、边界拒绝和持久队列租约fencing；本轮无新阻断",
    "ALN-10-E": "真实候选输出末行严格JSON、逐例/政策/timeout摘要绑定、门禁一致性与版本拒绝，1015f54修复",
}
gaps = {
    "ALN-26-A": "线上遥测/API/UI与真实预算、跨进程规划去重仍待；此前5f09fb8补丁独立确认另计",
    "ALN-27-A": "真实运行版本/数据集权威绑定、LLM前后质量和用户/项目可见性相关闭环仍待；旧修复待独立确认",
    "ALN-27-B": "生产执行/动态展开与组合收益证据、旧6ae4b30补丁独立确认仍待",
    "ALN-09-H": "OS级隔离、跨平台文件描述符、资源/输出内存上限和生产并发仍待；旧fde8f89补丁须独立确认",
    "ALN-10-E": "1015f54待独立复核；v2仅历史、v3需重跑；生产任务集/独立oracle、发布门禁自动消费与真实收益监测仍待",
}
reviews = {cid: {"date": "2026-10-27", "reviewer": "codex-runtime", "source_commit": source,
    "source_tree": records["final-validation"]["source_tree"], "report": report, "evidence": evidence,
    "scope": scopes[cid], "status": "changes_fixed_pending_independent_recheck" if cid == "ALN-10-E"
    else "technical_recheck_complete_with_acceptance_gaps", "result": "固定16相关模块278通过；非全量/设备/生产",
    "remaining_acceptance_gaps": gaps[cid], "independent_acceptance_added": False} for cid in scopes}
entry = {"date": "2026-10-27", "source_commit": source, "fix_commits": [source], "report": report,
    "validation_records": records, "cards": reviews,
    "results": {"initial_negative": "12 failed, 2 passed", "focused": "57 passed",
                "final_new_probes": "15 passed", "fixed_related": "278 passed"},
    "benchmark_version": "host-bench/3", "owned_validation_scopes_removed": 4,
    "not_run": ["full backend", "client/TypeScript", "device/APK", "whole-host CPU saturation", "production dataset/providers", "OS security isolation"],
    "digest_boundary": "Consistency binding only, not signature/provenance authentication or an independent result oracle",
    "independent_acceptance_added": False, "foreign_work_preserved": True,
    "log_sha256_lf": {p.name: hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest() for p in folder.glob("*.txt")}}
(folder / "committed-evidence.json").write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
rows = "\n".join(f"| {cid} | {scopes[cid]} | {gaps[cid]} |" for cid in scopes)
(root / report).write_text(f"""# 其余五项与候选基准证据复核

2026-10-27，codex-runtime。接续17e2bbc指定11项后，复核26-A/27-A/27-B/09-H/10-E；原owner/reviewer保持，本轮不代替其他人的终审。期间79db521二审记录已入库并保留；26-B/29-C/12-V及29-E更新发布线在途内容不混入。固定修复 **{source}**。

## 确认问题与修复

| 编号 | 级别 | 触发、影响与处理 |
|---|---|---|
| OB-01 | P2 | _last_json回溯寻找任意JSON对象：候选先输出成功JSON、末尾再输出坏行/数组/false仍被接受；NaN/Infinity也被json.loads当成功值。现只解析最后非空行，要求严格JSON对象、有限浮点，拒绝NaN/Infinity和1e999溢出；前置日志与尾部空行保留 |
| OB-02 | P2 | 摘要漏绑实际逐用例status/ok/value与timeout/阈值，修改这些字段仍可复用旧摘要。现绑定两侧逐例语义、omissions/可复现标记、policy及timeout；仅执行耗时/机器噪声仍不绑定，保持相同语义复跑摘要稳定 |
| OB-03 | P2 | release_decision仅排inconclusive，若regressed/unknown没有regression_cases可被放行，且不核对摘要。现仅允许improved/neutral，要求字面可复现true、当前版本和匹配摘要；旧版不能重新计算摘要就冒称升级 |

基准协议升级 **host-bench/3**。v2报告保留历史，不自动转换为v3认可证据，需以新协议重跑。摘要是**一致性绑定**，不是来源签名或独立结果oracle；本模块仍只提供发布门禁输入，未因此接上生产发布/回滚执行器。

## 逐卡复核

| 卡片 | 本轮结果 | 剩余条件 |
|---|---|---|
{rows}

26-A已在plan_candidates重新收集并比较权威快照，不能把旧历史可伪造证据误报成现存缺陷。27-A质量对照以同专家/同逻辑任务为前提，命中率不冒称收益。27-B产出精确依赖草稿，不冒称已展开执行。09-H仍是风险降低环境，Python审计钩子不冒称OS强隔离。

## 验证与边界

原14项新探针修前 **12失败/2通过**；修后与既有候选/基准API专项 **57通过**。追加指数溢出后新15项 **15通过**。输出协议探针使用真实资产仓储与真实候选子进程；报告修改探针仅验证摘要一致性与门禁判定，不冒称恶意制品安全或来源认证。

固定1015f54 Git归档 **16相关模块278通过**，覆盖缺口规划、组合/资产、知识/上下文、候选子进程/沙箱/队列与基准/API。不是全量后端；本轮未重跑客户端/TS、设备/APK、整机CPU饱和或生产任务集/真实供应商。没有把上一轮469客户端当本轮新证据。

完整参数、归档/源码树及日志摘要见[固定证据](../../../{evidence})。四处本轮独占验证范围全部回收，旧缓存/依赖和其他会话内容保留。5卡review及原分工不变；新/旧自有补丁仍待独立确认，不关闭父包，不改发布基线。
""", encoding="utf-8")


def append(path, marker, body):
    target = root / path
    text = target.read_text(encoding="utf-8")
    if marker not in text:
        target.write_text(text.rstrip() + "\n\n" + marker + "\n\n" + body + "\n", encoding="utf-8")


append("docs/协作开发/交付/ALN-10-E-真实候选收益与依赖回归证据.md", "## 2026-10-27 基准输出与证据接续修复",
    "1015f54升级host-bench/3：只接受最后非空行严格JSON对象；摘要绑定逐用例语义及策略/timeout；发布输入核对摘要/版本并拒绝regressed/unknown。旧v2仅历史需重跑，非来源认证或生产发布接线。新15探针通过、固定相关278通过，详见[复核报告](../验收/2026-10-27-其余五项与基准证据复核.md)。新补丁待独立确认，原设计缺口保留。")
append("docs/协作开发/验收/2026-10-26-候选基准与隔离文件边界复核.md", "## 2026-10-27 v3接续说明",
    "本文fde8f89/host-bench/2与318通过保留为历史。接续1015f54修末行协议/非有限JSON、逐例与策略摘要绑定及门禁一致性，升级host-bench/3；固定16相关模块278通过，旧v2不能自动当新协议接受。见[接续报告](2026-10-27-其余五项与基准证据复核.md)。")
append("docs/协作开发/待接需求_2026-10-26.md", "## 二十四、其余五项与基准证据接续复核（2026-10-27）",
    "26-A/27-A/27-B/09-H/10-E已接续复核，1015f54修10-E末行JSON误接受、摘要漏绑逐例/策略和门禁一致性。host-bench/3，新15探针通过，固定16相关模块278通过；四处新验证范围回收。见[报告](验收/2026-10-27-其余五项与基准证据复核.md)。\n\nv2仅历史，需重跑才有v3证据；生产分布/独立oracle、发布门禁与回滚自动消费仍待。其他四卡覆盖内无新增阻断，线上缺口/预算、真实知识迁移与身份可见性、组合展开、OS隔离/并发仍按原卡接手；原分工与review保留，新旧补丁需独立确认。")
board_path = root / "docs/协作开发/workboard.json"
board = json.loads(board_path.read_text(encoding="utf-8"))
before = copy.deepcopy(board)
delta = {"cards": {}, "post_candidate_changes": [], "code_review_batches": []}
for card in board["dispatches"]:
    cid = card["id"]
    if cid not in scopes:
        continue
    assert card["status"] == "review"
    fields = {"technical_review": reviews[cid], "latest_code_review": {
        "id": "REVIEW-2026-10-27-OTHER-5", "date": "2026-10-27", "reviewer": "codex-runtime",
        "reviewed_commit": source, "report": report, "scope": scopes[cid], "status": reviews[cid]["status"]},
        "next_step": "本轮固定16模块278通过；" + gaps[cid] + "；" + report}
    if cid == "ALN-10-E":
        fields.update(review_verdict="changes_fixed_pending_independent_recheck", blocker=gaps[cid])
    item = {"date": "2026-10-27", "reviewer": "codex-runtime", "source_commit": source,
        "report": report, "log": evidence, "scope": scopes[cid], "result": "固定相关278通过，非完整接受"}
    history = {"date": "2026-10-27", "previous_technical_review": copy.deepcopy(card.get("technical_review")),
        "previous_latest_code_review": copy.deepcopy(card.get("latest_code_review")),
        "reason": "其他五卡接续复核，10-E新增v3修复；原审阅历史保留", "report": report}
    card.update(copy.deepcopy(fields))
    card["evidence"].append(item)
    card.setdefault("review_history", []).append(history)
    delta["cards"][cid] = {"fields": fields, "evidence": item, "history": history}
change = {"id": "review-fix-" + source[:12], "title": "fix(benchmark): 严格核验候选输出与证据摘要", "workpackage": "ALN-10",
    "source_commit": source, "files": subprocess.check_output(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", source], cwd=root, text=True, encoding="utf-8").splitlines(),
    "status": "source_registered_pending_validation", "candidate_included": False, "independent_review": False,
    "report": report, "scope": "host-bench/3严格输出/语义摘要/门禁输入；278相关，待独立复核"}
batch = {"id": "REVIEW-2026-10-27-OTHER-5", "date": "2026-10-27", "reviewer": "codex-runtime",
    "cards": list(scopes), "fixed_cards": ["ALN-10-E"], "fix_commit": source,
    "report": report, "evidence": evidence, "status": "technical_reviews_complete_fixes_pending_independent_recheck",
    "product_acceptance_added": False, "published_by_review": False, "scope": "其余五卡接续；278相关，新15探针，host-bench/3；原分工与缺口保留"}
for key, item in (("post_candidate_changes", change), ("code_review_batches", batch)):
    assert not any(c["id"] == item["id"] for c in board[key])
    board[key].append(item)
    delta[key].append(item)
assert {c["id"] for c, old in zip(board["dispatches"], before["dispatches"]) if c != old} == set(scopes)
board_path.write_text(json.dumps(board, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
(folder / "metadata-delta.json").write_text(json.dumps(delta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("registered five reviews; host-bench/3; 278 passed; four validation scopes removed")
