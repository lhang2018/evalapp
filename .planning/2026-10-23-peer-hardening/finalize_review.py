"""Attach follow-up repair evidence without accepting reviewer-authored fixes."""
import json
from pathlib import Path
root=Path(__file__).resolve().parents[2]
prefix=".planning/2026-10-23-peer-hardening/"
e=json.loads((root/prefix/"committed-evidence.json").read_text(encoding="utf-8"))
assert all(r["exit_code"]==0 for r in e["results"]), "Verification failures must be resolved first"
path=root/"docs/协作开发/workboard.json"
b=json.loads(path.read_text(encoding="utf-8"))
report="docs/协作开发/验收/2026-10-23-审核修复后续加固.md"
ids={"ALN-07-W", "ALN-18-C", "ALN-18-D", "ALN-18-F"}
for c in b["dispatches"]:
    if c["id"] not in ids:
        continue
    assert c["status"]=="review", c["id"]
    history=c.setdefault("review_fix_commits", [])
    for commit in [c.get("fix_commit"), e["source_commit"]]:
        if commit and commit not in history:
            history.append(commit)
    c["fix_commit"]=e["source_commit"]
    c["fix_status"]="reviewer_fixed_pending_independent_review"
    c["fix_author"]="codex-runtime"
    c["followup_report"]=report
    c["next_step"]="由其他审阅者复核7132180/1ff11c1及34b5fac后续加固；检查冲突保留、评测空候选/错误隔离及模式/预算契约，保持review，原整包与真实环境缺口保留"
    c["evidence"].append(dict(date="2026-10-23", scope="审核修复后续加固；不自批修复", source_commit=e["source_commit"], result="24项负向行为修复；26探针及193相关联合通过，固定全量1416/1", log=prefix+"committed-evidence.json", report=report))
for c in b["workpackages"]:
    if c["id"] in {"ALN-07", "ALN-18"}:
        c["next_step"]="7132180/1ff11c1及34b5fac后续修复均待其他审阅者复核；保留真实运行时接线、执行器消费/编程包集成与真实环境验收，整包未关闭"
b["baseline"]["latest_source_verification"] = dict(source_commit=e["source_commit"], source_tree=e["source_tree"], python_passed=1416, python_skipped=1, client_passed=166, typecheck="passed", scope="34b5fac固定git archive后端1416/1，线程异常警告升级错误；本轮26项独立探针与相关联合193通过；声明/台账/认领表通过。客户端同Git tree复用8c479b6的166/typecheck；未重跑真机，不替代独立接受或发布候选", evidence="../../"+prefix+"committed-evidence.json")
b["baseline"]["latest_repair_followup"] = dict(date="2026-10-23", author="codex-runtime", source_commit=e["source_commit"], cards=sorted(ids), result="fixed_pending_independent_review", report=report)
for name in ["evoagent/planning/contracts.py"]:
    if not any(s["path"]==name for s in b["shared_files"]):
        b["shared_files"].append(dict(path=name, stream="integration"))
b["updated_at"]="2026-10-23"
path.write_text(json.dumps(b, ensure_ascii=False, indent=2)+"\n",encoding="utf-8",newline="\n")
