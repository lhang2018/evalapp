"""Record eleven assigned reviews and an exact metadata patch for isolated staging."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
source = "e89b4aee1f6cb1e18ee49baa5a4e2246570ddc77"
report = "docs/协作开发/验收/2026-10-27-指定十一项接续审核.md"
evidence = ".planning/2026-10-27-assigned-review-2/committed-evidence.json"
coverage = json.loads((folder / "coverage.json").read_text(encoding="utf-8"))
records = {name: json.loads((folder / (name + ".json")).read_text(encoding="utf-8"))
           for name in ("negative-validation", "focused-validation", "final-validation", "validation")}
for record in records.values():
    owned = Path(record["owned_temp_directory"])
    assert owned.resolve().parent == (root / "outputs").resolve()
    assert record["temporary_data_removed"] and not owned.exists()
assert records["final-validation"]["snapshot_commit"] == source and records["final-validation"]["exit_code"] == 0
assert records["validation"]["commit"] == source and not any(records["validation"]["results"].values())
cases = list(ET.parse(folder / "junit.xml").getroot().iter("testcase"))
assert not any(c.find("failure") is not None or c.find("error") is not None for c in cases)
assert sum(c.find("skipped") is None for c in cases) == 487
assert not any(c.find("skipped") is not None for c in cases)
# Pytest deselection is absent from JUnit rather than reported as skipped.
assert "487 passed, 1 deselected" in (folder / "final-tests.txt").read_text(encoding="utf-8")
assert "# pass 469" in (folder / "client-tests.txt").read_text(encoding="utf-8")
gaps = {
    "ALN-18-L": "语义LSP/真实专家自动装配仍缺；e89b4ae新注解拒绝补丁待独立确认",
    "ALN-19-A": "真实可播放供应商产物/版本、价格与账单、持久预算及生产凭证未验",
    "ALN-30-B": "项目工作区节点、全量成果下载、固定APK与模型实际禁用链仍待",
    "ALN-16-Q": "受信主体/授权证据联验、大导出/清理期与设备仍待；跨库删除仅维护窗口",
    "ALN-15-E": "index.js实际HeadlessTask未注入sampler/highWater；策略下发/Kotlin/新APK真机未验",
    "ALN-13-S": "既有a435ce1/f973dcc调度补丁待独立确认；真实sampler/主动预算及运行中治理/生产长跑仍待",
    "ALN-09-I": "candidate_id/dataset到权威仓库的精确绑定、生产并发与完整管理面仍缺",
    "ALN-18-H": "真实任务自动选专家→安装→执行→失败回滚再检闭环、真实项目升级恢复仍待",
    "ALN-25-C": "真实资源/预算账户、实际费用settle回接、权威并发/运行中停止及核心质量延迟仍待",
    "ALN-27-C": "正式验收项2用户/项目身份可见性绑定仍缺，真实同任务集质量/逆向迁移与多进程仍待",
    "ALN-24-F": "真实主体策略→模型/manifest、Grant/撤销及App管理仍缺；e89b4ae初始化事务补丁待独立确认",
}
scope = {
    "ALN-18-L": "可变参数注解缺口修复，AST重构/诊断及编码工具消费者",
    "ALN-19-A": "本地HTTP供应商/未知费用、预算预留与拒绝、发布证据契约",
    "ALN-30-B": "固定源码成果视图/记忆控制及完整离线、设备、更新测试与TS",
    "ALN-16-Q": "聚合证据、授权导出下载、删除预览摘要/拒绝及残留对账",
    "ALN-15-E": "原生采样TS解析和后台helper；真实index.js接线走读，非Kotlin/设备接受",
    "ALN-13-S": "完整在途/目标账本、时钟/策略、宿主主动off和资源准入",
    "ALN-09-I": "候选宿主/持久队列、host-bench/2失败边界及受控API",
    "ALN-18-H": "精确编码包/独立项目注册表、真实语法工具、宿主权限与拒绝",
    "ALN-25-C": "真实预留协议/未知提交、幂等、资源准入与停止策略",
    "ALN-27-C": "知识持久化/新鲜撤销/事务与迁移、实际上下文注入边界",
    "ALN-24-F": "建表事务所有权修复、跨连接提交/回滚和主体分区迁移拒绝",
}
reviews = {}
for card, modules in coverage["cards"].items():
    relevant = [c for c in cases if any(c.attrib["classname"] == "tests." + module
                or c.attrib["classname"].startswith("tests." + module + ".") for module in modules)]
    reviews[card] = {"date": "2026-10-27", "reviewer": "codex-runtime", "reviewed_source_commit": source,
        "source_tree": records["final-validation"]["source_tree"], "report": report, "evidence": evidence,
        "status": "changes_fixed_pending_independent_recheck" if card in ("ALN-18-L", "ALN-24-F")
                  else "technical_recheck_complete_with_acceptance_gaps",
        "scope": scope[card], "checks": {"backend_modules": modules,
            "backend_passed": sum(c.find("skipped") is None for c in relevant),
            "backend_skipped": sum(c.find("skipped") is not None for c in relevant),
            "client_passed": 469 if card in ("ALN-30-B", "ALN-15-E") else 0,
            "typecheck_exit": 0 if card in ("ALN-30-B", "ALN-15-E") else None},
        "remaining_acceptance_gaps": gaps[card], "independent_acceptance_added": False,
        "prior_codex_fixes_require_independent_review": True}
entry = {"date": "2026-10-27", "source_commit": source, "fix_commits": [source], "report": report,
    "validation_records": records, "cards": reviews, "results": {"negative": "8 failed, 1 passed", "focused": "77 passed",
    "fixed_backend": "487 passed, 1 deselected", "fixed_client": "469 passed, TypeScript exit 0"},
    "owned_validation_scopes_removed": 4, "independent_acceptance_added": False,
    "not_run": ["full backend", "whole-host CPU saturation", "Kotlin compiler", "device/APK", "production providers"],
    "foreign_work_preserved": True,
    "log_sha256_lf": {p.name: hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest() for p in folder.glob("*.txt")}}
(folder / "committed-evidence.json").write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
(folder / "assigned-review.json").write_text(json.dumps({"cards": reviews, "source_commit": source,
    "next_stage": "既有独立终审/缺口验收；本轮不新增接受", "not_additive": True}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
rows = []
for card, review in reviews.items():
    count = str(review["checks"]["backend_passed"]) + "相关后端" if review["checks"]["backend_modules"] else "469客户端/TS"
    rows.append(f"| {card} | {count} | {'新缺陷已修，补丁待独立复核' if card in ('ALN-18-L','ALN-24-F') else '覆盖内未发现新增阻断'}；{gaps[card]} |")
(root / report).write_text(f"""# 指定十一项接续审核

2026-10-27，codex-runtime。按当前台账明确reviewer=codex-runtime且status=review的11卡逐项复核，起点35cd61d，固定修复 **{source}**。此前13项交接包还含30-A/25-B二审，此次不重复计入新指派。

## 新问题与修复

| 编号 | 级别 | 影响与处理 |
|---|---|---|
| AR-18L-01 | P2 | 参数注解扫描漏掉*args/**kwargs。class Helper重命名可被批准，却留下字符串前向注解Helper，实际typing.get_type_hints从正常解析变NameError。现纳入两类可变参数，沿用字符串注解需澄清规则，不盲改字符串；非字符串真实类型注解正常重命名 |
| AR-24F-01 | P2 | ScopePolicyStore._ensure无条件commit，会提前提交宿主尚未决定的事务；自身初始化第3张表失败会留下前两张表。现复用既有事务所有权装饰器，有父事务就加入且不提交；无父事务时自主事务整批建表/失败回滚，可重试 |

9条新探针修前 **8失败/1通过**，聚焦 **77通过**；包括已建/未建schema、父事务最后commit/rollback四组合，独立连接观察未提交数据，故障建表后零半表与成功重试，实际前后注解解析。未改变编码工具权限或主体可见性设计。

## 逐卡结论

| 指派卡 | 本轮覆盖 | 技术结论与仍需满足的验收条件 |
|---|---|---|
""" + "\n".join(rows) + f"""

固定e89b4ae全新Git归档，后端30个相关模块 **487通过/1未选**（整机CPU饱和不重跑），完整客户端 **469通过/TypeScript通过**。各卡模块有交集，18-L/24-F还共用组合探针，表内计数不可相加；30-B/15-E共用同一客户端运行，不算938。共享资源准入24项另含于487。客户端固定源码排除了更新发布线当前未提交修改，不能作为那些在途变更的验收。

相较上次f973dcc，最新已提交消费变化集中于controller/resource_admission/state_machine及设备dispatch链；本轮核对这些共享变化，并逐域复核未改动的服务/API。其余九卡在上述覆盖内未发现新增阻断，**已有正式缺口仍阻塞完整接受**；不以历史测试替代新源证据，也不把组件通过当生产闭环。

原owner/reviewer/final_reviewer均保留，11卡保持review。两处新补丁及本会话既有补丁交另一审阅人确认；本轮没有新增独立接受，不关闭父包，不改既有发布基线。

## 证据与清理

[固定证据](../../../{evidence})与同目录coverage.json/junit.xml逐卡记录模块、参数、源码树/归档与结果；assigned-review.json是本次11项的机器交接。不是后端全量，Kotlin/新APK/真机、真实供应商/LLM与生产并发均未验。

四处本轮新建验证范围全部回收，原node_modules、旧缓存、UI/matrix等保留。26-B/29-C/12-V指派变更、29-E与Android/update/release/channels在途修改保留，用独立暂存索引提交自己的卡片增量，避免混入其他会话。
""", encoding="utf-8")


def append(path, marker, body):
    target = root / path
    text = target.read_text(encoding="utf-8")
    if marker not in text:
        target.write_text(text.rstrip() + "\n\n" + marker + "\n\n" + body + "\n", encoding="utf-8")


for card, title in (("18-L", "跨文件语义与LSP能力扩展"), ("24-F", "多主体记忆范围策略与迁移隔离")):
    append(f"docs/协作开发/交付/ALN-{card}-{title}.md", "## 2026-10-27 指派接续审核",
        f"e89b4ae补齐{'可变参数字符串注解澄清' if card=='18-L' else '初始化建表事务所有权/自身失败回滚'}，新9项组合探针修前8失败/1通过，聚焦77通过。固定相关487/1未选，客户端469/TS，具体范围见[十一项审核](../验收/2026-10-27-指定十一项接续审核.md)。新补丁待独立复核，原卡正式验收缺口与review保留。")
append("docs/协作开发/待接需求_2026-10-26.md", "## 二十三、指定十一项接续审核（2026-10-27）",
    "当前明确指派codex的11张review卡已逐项接续，e89b4ae修复18-L可变参数字符串注解误批准、24-F建表提交宿主事务/自身失败残表。固定相关487通过/1整机饱和未选，客户端469/TS通过，四处新验证范围回收。逐卡缺口见[报告](验收/2026-10-27-指定十一项接续审核.md)。\n\n两处新补丁需独立确认；其余九卡本次覆盖内无新增阻断，既有主体/知识可见性、候选权威绑定、真实资源/费用/运行中治理、专家自动装配、LSP与设备/供应商等条件仍须按卡完成。原分工与review保留，11卡不冒称完整接受。")
board_path = root / "docs/协作开发/workboard.json"
board = json.loads(board_path.read_text(encoding="utf-8"))
before = copy.deepcopy(board)
delta = {"cards": {}, "post_candidate_changes": [], "code_review_batches": []}
for card in board["dispatches"]:
    if card["id"] not in reviews:
        continue
    cid = card["id"]
    assert card["reviewer"] == "codex-runtime" and card["status"] == "review"
    evidence_item = {"date": "2026-10-27", "reviewer": "codex-runtime", "source_commit": source,
        "scope": scope[cid], "result": "接续审核487/1未选与469/TS，逐卡范围见technical_review；非完整接受",
        "report": report, "log": evidence}
    history_item = {"date": "2026-10-27", "previous_technical_review": copy.deepcopy(card.get("technical_review")),
        "reason": "明确指派11卡接续审核，新源证据与新缺陷补丁；旧审核保留", "report": report}
    fields = {"technical_review": reviews[cid], "latest_code_review": {
        "id": "REVIEW-2026-10-27-ASSIGNED-11", "date": "2026-10-27", "reviewer": "codex-runtime",
        "reviewed_commit": source, "scope": scope[cid], "report": report, "status": reviews[cid]["status"]},
        "next_step": f"本轮指定接续审核已完成（固定487/1未选，客户端469/TS，逐卡范围见报告）；{gaps[cid]}；{report}"}
    if cid in ("ALN-18-L", "ALN-24-F"):
        fields.update(review_verdict="changes_fixed_pending_independent_recheck", blocker=gaps[cid])
    card.update(copy.deepcopy(fields))
    card["evidence"].append(evidence_item)
    card.setdefault("review_history", []).append(history_item)
    delta["cards"][cid] = {"fields": fields, "evidence": evidence_item, "history": history_item}
change = {"id": "review-fix-" + source[:12], "title": "fix(review): 补全可变参数注解与策略建表事务边界", "workpackage": "ALN-24",
    "source_commit": source, "files": subprocess.check_output(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", source], cwd=root, text=True, encoding="utf-8").splitlines(),
    "status": "source_registered_pending_validation", "candidate_included": False, "independent_review": False,
    "report": report, "scope": "18-L/24-F接续缺陷修复；明确指派11项逐卡复核，待独立补丁确认"}
batch = {"id": "REVIEW-2026-10-27-ASSIGNED-11", "date": "2026-10-27", "reviewer": "codex-runtime",
    "cards": list(reviews), "fixed_cards": ["ALN-18-L", "ALN-24-F"], "fix_commit": source,
    "report": report, "evidence": evidence, "status": "technical_reviews_complete_fixes_pending_independent_recheck",
    "product_acceptance_added": False, "published_by_review": False, "scope": "明确指定11卡；487/1未选，469客户端/TS；原分工与缺口保留"}
for key, item in (("post_candidate_changes", change), ("code_review_batches", batch)):
    assert not any(c["id"] == item["id"] for c in board[key])
    board[key].append(item)
    delta[key].append(item)
assert {c["id"] for c, old in zip(board["dispatches"], before["dispatches"]) if c != old} == set(reviews)
board_path.write_text(json.dumps(board, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
(folder / "metadata-delta.json").write_text(json.dumps(delta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("registered eleven reviews, two fixed cards; backend487/1, client469/TS; four validation scopes removed")
