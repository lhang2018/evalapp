"""Attach fixed-source evidence without promoting new code to accepted."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PREFIX = OUT.relative_to(ROOT).as_posix()
SOURCE = "36d078aad0470a3c54f3abe88e6977bd02d39369"
FIRST_FIX = "a0de59535b5f8faa7b87ce265fc9dd17e9cd80b6"
REPORT = "docs/协作开发/验收/2026-10-26-知识持久化与上下文注入复核.md"
EVIDENCE = PREFIX + "/committed-evidence.json"
records = {p.name:json.loads(p.read_text(encoding="utf-8")) for p in OUT.glob("*-validation.json")}
assert len(records) == 8 and all(r["temporary_data_removed"] for r in records.values())
fixed = records["final-fixed-validation.json"]
assert fixed["snapshot_commit"] == SOURCE and fixed["exit_code"] == 0
expected = {"negative.txt":"18 failed", "focused.txt":"46 passed",
            "expanded.txt":"1 failed, 50 passed", "runtime.txt":"51 passed",
            "final-focused.txt":"53 passed", "fixed.txt":"340 passed",
            "timestamp-negative.txt":"1 failed", "final-fixed.txt":"341 passed"}
for name, result in expected.items():
    assert result in (OUT/name).read_text(encoding="utf-8")
summary = dict(source_commit=SOURCE,source_tree=fixed["source_tree"],related_python_passed=341,
    fix_commits=[FIRST_FIX,SOURCE],
    full_backend_rerun=False,client_rerun=False,real_llm_verified=False,device_verified=False,
    multi_principal_knowledge_binding_verified=False,acceptance_promoted=False,
    temporary_scopes_removed=8,records=records,
    intermediate_harness_error="EvalCase(input=...) corrected to input_goal; 1 failed/50 passed retained",
    log_sha256_lf={name:hashlib.sha256((OUT/name).read_bytes().replace(b"\r\n",b"\n")).hexdigest() for name in expected})
(OUT/"committed-evidence.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

path = ROOT/"docs/协作开发/workboard.json"
board = json.loads(path.read_text(encoding="utf-8"))
before = {r["id"]:tuple(r.get(k) for k in ("owner","reviewer","status")) for r in board["dispatches"]}
batch_id = "REVIEW-2026-10-26-KNOWLEDGE-CONSUMPTION"
batch = dict(id=batch_id,date="2026-10-26",reviewer="codex-runtime",cards=["ALN-27-A","ALN-27-C"],
    fixed_cards=["ALN-27-A","ALN-27-C"],fix_commit=SOURCE,fix_commits=[FIRST_FIX,SOURCE],report=REPORT,evidence=EVIDENCE,
    status="fixes_delivered_pending_independent_recheck",product_acceptance_added=False,
    published_by_review=False,scope="知识鲜度/事务/迁移、注入控制和固定任务质量；固定相关341过，非全量/真实LLM/身份隔离接受")
board["code_review_batches"] = [r for r in board["code_review_batches"] if r["id"] != batch_id] + [batch]
notes = {
    "ALN-27-A":"公共知识验证、NaN门禁和固定任务比较缺陷已修，原接受重新review；重复/非布尔样本拒绝、不同任务清单unproven。真实运行版本/数据集绑定和LLM质量仍待。",
    "ALN-27-C":"跨连接知识鲜度/撤销、写失败和宿主事务、整批迁移回滚、不可信/评测注入控制、压缩后计数已修。正式验收项2用户/项目身份绑定仍未实现；不能以适用域过滤代替身份隔离。"}
for row in board["dispatches"]:
    if row["id"] not in notes:
        continue
    if row["id"] == "ALN-27-A" and row["status"] == "accepted":
        row.setdefault("review_history",[]).append(dict(date="2026-10-26",previous_status="accepted",
            previous_reviewer=row.get("reviewer"),reason="负向探针确认质量比较未约束同一任务清单等公共语义缺陷；本轮修复待他人复核",report=REPORT))
        row["status"] = "review"
    if row["id"] == "ALN-27-C":
        row["blocker"] = "正式验收项2：知识可见性尚未绑定既有用户/项目身份；真实固定任务/run版本与LLM前后质量仍未验收。"
    row["latest_code_review"] = batch
    row["next_step"] = notes[row["id"]] + " 固定相关341过，本轮新修复待另一审阅人复核；不关闭父包。"
    evidence = row.setdefault("evidence",[])
    evidence[:] = [r for r in evidence if not isinstance(r,dict) or r.get("log") != EVIDENCE]
    evidence.append(dict(date="2026-10-26",source_commit=SOURCE,source_tree=fixed["source_tree"],
        result=row["next_step"],log=EVIDENCE,delivery=REPORT))
for row in board["dispatches"]:
    keys = ("owner","reviewer") if row["id"] == "ALN-27-A" else ("owner","reviewer","status")
    assert tuple(row.get(k) for k in keys) == before[row["id"]][:len(keys)]
if not any(r.get("source_commit") == SOURCE for r in board["post_candidate_changes"]):
    paths = subprocess.check_output(["git","diff-tree","--no-commit-id","--name-only","-r",SOURCE],
        cwd=ROOT,text=True,encoding="utf-8").splitlines()
    board["post_candidate_changes"].append(dict(id="discovered-"+SOURCE[:12],workpackage="ALN-27",
        title="fix(knowledge): 规范化撤销时间戳保证迁移摘要稳定",source_commit=SOURCE,
        status="source_registered_pending_validation",files=paths,candidate_included=False,
        independent_review=False,report=REPORT,scope="27-A/27-C修复，固定相关341；身份/项目隔离和真实LLM正式接受未完成"))
board["updated_at"] = "2026-10-26"
path.write_text(json.dumps(board,ensure_ascii=False,indent=1)+"\n",encoding="utf-8")

for name, card in (("ALN-27-A-故障经验沉淀与跨专家迁移验证.md","ALN-27-A"),
                   ("ALN-27-C-知识持久化真实注入与迁移质量验证.md","ALN-27-C")):
    path = ROOT/"docs/协作开发/交付"/name
    body = path.read_text(encoding="utf-8")
    if "2026-10-26 知识消费接续复核" in body:
        body = body.replace("修复a0de595，固定相关340过", "修复a0de595/36d078a，固定相关341过")
        path.write_text(body,encoding="utf-8")
        continue
    heading, rest = body.split("\n",1)
    body = (heading + "\n\n> 2026-10-26 知识消费接续复核：" + notes[card]
        + " 修复a0de595/36d078a，固定相关341过；新增修复待另一审阅人复核。下文为原交付历史记录，最新行为与正式接受缺口见"
        + "[复核报告](../验收/2026-10-26-知识持久化与上下文注入复核.md)，不扩大接受。\n"+rest)
    path.write_text(body,encoding="utf-8")

path = ROOT/"docs/协作开发/待接需求_2026-10-26.md"
body = path.read_text(encoding="utf-8")
if "## 十五、知识持久化与上下文消费复核" in body:
    head, marker, tail = body.partition("## 十五、知识持久化与上下文消费复核")
    tail = tail.replace("修复a0de595：固定相关340项通过", "修复a0de595/36d078a：固定相关341项通过")
    tail = tail.replace("6处新受控临时范围已回收", "8处新受控临时范围已回收")
    path.write_text(head+marker+tail,encoding="utf-8")
if "## 十五、知识持久化与上下文消费复核" not in body:
    body += """

## 十五、知识持久化与上下文消费复核

修复a0de595/36d078a：固定相关341项通过，非全量后端/客户端/真实LLM验证。七类问题及原始失败记录见[复核报告](验收/2026-10-26-知识持久化与上下文注入复核.md)。8处新受控临时范围已回收，原在途内容/缓存/依赖保留。

| 范围 | 当前结果 / 下一步 |
|---|---|
| 持久知识状态与事务 | 跨连接撤销可见，陈旧写者不覆盖ID/重复版本；SQL失败不假报内存状态，不提交宿主父事务，App共用db锁 |
| 迁移 | 固定源快照+目标整批事务/内存回滚；失败不留半库。逆向/备份恢复、历史批量与持久批次审计仍待 |
| 上下文注入 | 不可信内容/来源排除且manifest留原因；评测关闭记忆同时排除历史知识；计数按压缩后保留条目。真实控制器消息已测，模型仍Mock |
| 迁移质量 | 固定同一逻辑任务清单、结果布尔、重复拒绝、真实任务终态；缺清单一致性时unproven。真实run版本/数据集绑定与LLM前后质量仍待 |
| **27-C正式验收项2** | **仍未接用户/项目身份可见性，当前为全局知识库；须复用既有身份/授权后补拒绝与正常共享证据，不能把适用域过滤当身份隔离** |

27-A原accepted重新review；27-C维持review并显式列接受阻塞。原owner/reviewer保留，新修复待另一审阅人复核，父包/基线不因本轮测试关闭或发布。
"""
    path.write_text(body,encoding="utf-8")
print("Registered 341 related passes; 27-A reopened, 27-C identity acceptance gap explicit; 8 new scopes removed")
