"""Record scoped review outcomes without closing parent packages."""
import json
from pathlib import Path
root = Path(__file__).resolve().parents[2]
prefix = ".planning/2026-10-23-peer-review/"
path = root / "docs/协作开发/workboard.json"
b = json.loads(path.read_text(encoding="utf-8"))
e = json.loads((root / prefix / "committed-evidence.json").read_text(encoding="utf-8"))
report = "docs/协作开发/验收/2026-10-23-其他作者待审卡独立审核.md"
original = json.loads((root / prefix / "scope.json").read_text(encoding="utf-8"))
ids = [c["id"] for c in original] + ["ALN-07-W"]
accepted = {"ALN-06-A", "ALN-11-A", "ALN-16-A"}
findings = {"ALN-03-S": ["PR-06"], "ALN-05-M": ["PR-04"], "ALN-07-P": ["PR-01"], "ALN-08-M": ["PR-05"], "ALN-07-W": ["PR-07"]}
for i in "ABCDEF":
    findings["ALN-18-" + i] = ["PR-02", "PR-03"]
for c in b["dispatches"]:
    if c["id"] not in ids:
        continue
    if c["id"] == "ALN-07-W":
        (root / prefix / "additional-scope.json").write_text(json.dumps(c, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    ok = c["id"] in accepted
    c.update(status="accepted" if ok else "review", reviewer="codex-runtime", review_result="accepted" if ok else "changes_requested", review_report=report)
    c["review_note"] = ("2026-10-23 独立接受既定切片；06墓碑/证据门禁重验，11/16依赖R卡已有claude-main独立接受及第三方探针重放；不重批本人R修复，不表示整包或新增真机通过" if ok else "2026-10-23 独立探针发现缺陷，审阅人已修复；新修复须交其他审阅者复核，不自行批准")
    c["blocker"] = None
    c["evidence"].append(dict(date="2026-10-23", scope="其他作者原切片独立审核；新修复不自批", reviewed_commit="4a73547111e25bfb15e629b3a695346d7eca25f5" if c["id"] != "ALN-07-W" else "7132180a49f03fdafd4ca1b8e43b8d88f54594dd", source_commit=e["source_commit"], result="既定切片accepted" if ok else "changes_requested; repaired_pending_independent_review", report=report, log=prefix + "committed-evidence.json"))
    if ok:
        c["fix_status"] = "independently_verified_scoped_slice"
        c["next_step"] = {"ALN-06-A": "既定资产基础切片已接受；运行时精确选择/消费、Android安装/退役另验", "ALN-11-A": "既定切片已接受，11-R独立接受保持；补真机故障回滚、自动健康探针、执行器消费活动workflow", "ALN-16-A": "既定契约切片已接受，16-R独立接受保持；端侧EvidenceEvent、实际仓储装配及导出删除另验"}[c["id"]]
    else:
        c.update(fix_status="reviewer_fixed_pending_independent_review", fix_author="codex-runtime", fix_commit=e["source_commit"] if c["id"] == "ALN-07-W" else "7132180a49f03fdafd4ca1b8e43b8d88f54594dd", peer_findings=findings[c["id"]])
        c["next_step"] = "由其他审阅者复核修复提交和负向探针（" + ",".join(findings[c["id"]]) + "）；通过前保持review，原整包/真实环境缺口保留"
for p in b["workpackages"]:
    if p["id"] in {"ALN-03", "ALN-05", "ALN-07", "ALN-08", "ALN-18"}:
        p["delivery_state"] = "needs_recheck"
        p["next_step"] = "本轮独立审核发现问题已修复，待其他审阅者复核7132180/1ff11c1；既有后续接线与真实环境验收保留，不关闭整包"
    elif p["id"] in {"ALN-06", "ALN-11", "ALN-16"}:
        p["delivery_state"] = "partial_verified"
        p["next_step"] = "原A切片已独立接受；运行时消费、端侧接线与实际环境验收仍按原任务卡推进，整包未关闭"
review = dict(date="2026-10-23", reviewer="codex-runtime", status="complete", reviewed_commit="4a73547111e25bfb15e629b3a695346d7eca25f5", additional_reviewed_commit="7132180a49f03fdafd4ca1b8e43b8d88f54594dd", cards=ids, accepted=sorted(accepted), changes_requested=[i for i in ids if i not in accepted], repair_source_commit=e["source_commit"], report=report, independence="新修复由审阅人编写，11张卡保持review待其他审阅者复核；自己的14/15-R不在本轮接受范围")
review["repair_paths"] = b["peer_review_batch"]["repair_paths"] + ["evoagent/planning/matching.py", "evoagent/evolution/routing_eval.py"]
b["peer_review_batch"] = review
b["baseline"]["latest_peer_review"] = {k:v for k,v in review.items() if k not in {"repair_paths", "cards"}}
b["baseline"]["latest_source_verification"] = dict(source_commit=e["source_commit"], source_tree=e["source_tree"], python_passed=1390, python_skipped=1, client_passed=166, typecheck="passed", scope="固定Git archive后端1390/1；客户端同Git tree复用8c479b6的166/typecheck；35项独立探针修复前失败32项，修复后全过。元数据旧渲染差异另在文档提交修复；不替代真实环境、独立接受或发布候选", evidence="../../" + prefix + "committed-evidence.json")
for name in review["repair_paths"]:
    if not any(s["path"] == name for s in b["shared_files"]):
        b["shared_files"].append(dict(path=name, stream="integration"))
b["updated_at"] = "2026-10-23"
path.write_text(json.dumps(b, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
