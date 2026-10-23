"""Publish delivery metadata without accepting the author's own changes."""
import json,re
from pathlib import Path
root=Path(__file__).resolve().parents[2]
prefix=".planning/2026-10-23-aln05-integration/"
e=json.loads((root/prefix/"committed-evidence.json").read_text(encoding="utf-8"))
assert all(r["exit_code"]==0 for r in e["results"]), "Resolve verification failures before delivery"
match=re.search(r"(\d+) passed, (\d+) skipped",(root/prefix/"backend-committed.txt").read_text(encoding="utf-8"))
passed,skipped=map(int,match.groups())
report="docs/协作开发/交付/ALN-05-I-记忆身份与清单运行时接线.md"
path=root/"docs/协作开发/workboard.json"
b=json.loads(path.read_text(encoding="utf-8"))
shared=["evoagent/core/task.py","evoagent/core/controller.py","evoagent/core/persistence.py","evoagent/app.py",
        "evoagent/config.py","evoagent/api/auth.py","evoagent/api/server.py","evoagent/api/goals_api.py",
        "evoagent/api/memory_context_api.py","evoagent/core/schema/memory_context.py","evoagent/memory/binding.py",
        "evoagent/memory/store.py","evoagent/memory/context.py","FEATURES.md","IMPLEMENTATION.md"]
for c in b["dispatches"]:
    if c["id"]=="ALN-05-I":
        c.update(status="review",delivery=report,delivered_commit=e["source_commit"],preparation_status="delivered",shared_files=shared,
                 next_step="独立审阅主体白名单、Grant有效性/不扩域、旧任务竞争回填和实际清单；本卡不自批，不替代05-M原审阅及Android/隐私仓储验收")
        c["evidence"].append(dict(date="2026-10-23",scope="真实App/Controller/SQLite/HTTP及重启；内置Mock模型与本地工具，无真机/供应商",source_commit=e["source_commit"],source_tree=e["source_tree"],result=f"44 new consumers/probes; backend{passed}/{skipped}; client166/typecheck reused identical tree; declarations/ledgers/board/gap/alignment passed",log=prefix+"committed-evidence.json",delivery=report))
    if c["id"]=="ALN-05-M":
        history=c.setdefault("review_fix_commits",[])
        for commit in [c.get("fix_commit"),"5847f3f0766279eacdda23162be82cb19f30da9f",e["source_commit"]]:
            if commit and commit not in history:history.append(commit)
        c.update(fix_commit=e["source_commit"],fix_author="codex-runtime",fix_status="reviewer_fixed_pending_independent_review",consumer_delivery=report,
                 next_step="独立复核原05-M、7132180及05-I的owner链/去重冲突分区/清单正文引用化修复；运行时消费者已交付，仍不自批或关闭整包")
        c["evidence"].append(dict(date="2026-10-23",scope="05-I实际消费暴露的owner链、分区及Manifest正文问题修复",source_commit=e["source_commit"],result="原接口固定基点4 failed，修复后通过；作者修复待独立审阅",log=prefix+"before-fixed.txt",delivery=report))
        if c["status"]=="accepted" and c.get("third_party_review",{}).get("followup_fix_verified")=="5847f3f":
            c["fix_commit"]="5847f3f0766279eacdda23162be82cb19f30da9f"
            c["fix_status"]="independently_verified_through_5847f3f"
            c["review_note"]="原切片独立审阅及第三方修复接受范围保留；05-I新消费者接线不一并接受"
            c["next_step"]="05-M既定切片与覆盖修复已接受；05-I绑定/权限/迁移消费者另行review，整包未关闭"
            c["consumer_review"]=dict(card="ALN-05-I",status="review",source_commit=e["source_commit"],report=report)

for package in b["workpackages"]:
    if package["id"]=="ALN-05":
        package["delivery_state"]="needs_recheck"
        package["next_step"]="05-I已交付Task身份/主链读写及真实清单，和05-M修复按第三方范围接受，05-I待独立审阅；Android离线缓存/失效、隐私仓储与完整租户化另验，整包未关闭"
queue=b["execution_queue"]
queue["items"]=[item for item in queue["items"] if item["id"]!="ALN-05-I"]
queue["active_task"]=None;queue["next_task"]="ALN-11-W"
b["baseline"]["latest_source_verification"]=dict(source_commit=e["source_commit"],source_tree=e["source_tree"],python_passed=passed,python_skipped=skipped,client_passed=166,typecheck="passed",scope=f"ALN-05-I固定Git archive后端{passed}/{skipped}；44项新增实际消费者/探针；线程异常警告升级错误，声明/台账/认领/缺口映射/对齐覆盖通过；客户端同Git tree沿用8c479b6的166/typecheck，未重跑真机，不替代独立接受或候选发布",evidence="../../"+prefix+"committed-evidence.json")
b["baseline"]["latest_feature_delivery"]=dict(card="ALN-05-I",source_commit=e["source_commit"],status="review",report=report)
for name in shared:
    if not any(item["path"]==name for item in b["shared_files"]):
        b["shared_files"].append(dict(path=name,stream="integration"))
b["updated_at"]="2026-10-23"
path.write_text(json.dumps(b,ensure_ascii=False,indent=2)+"\n",encoding="utf-8",newline="\n")
