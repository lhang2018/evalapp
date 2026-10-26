"""Register fixed-source results; preserve concurrent owners and statuses."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PREFIX = OUT.relative_to(ROOT).as_posix()
SOURCE = "093c561e2c5d7a9920d6903d02a11c979659bad0"
REPORT = "docs/协作开发/验收/2026-10-26-证据消费与记忆策略接续复核.md"
EVIDENCE = PREFIX + "/committed-evidence.json"
records = {p.name: json.loads(p.read_text(encoding="utf-8"))
           for p in OUT.glob("*-validation.json")}
client = json.loads((OUT/"validation.json").read_text(encoding="utf-8"))
assert len(records) == 4
assert all(row["temporary_data_removed"] for row in records.values())
assert client["temporary_data_removed"] and client["dependency_junction_removed"]
assert client["installed_dependencies_preserved"] and client["commit"] == SOURCE
assert client["results"] == {"client-tests": 0, "typecheck": 0}
final = records["fixed-validation.json"]
assert final["snapshot_commit"] == SOURCE and final["exit_code"] == 0
assert final["source_tree"] == client["source_tree"]
expected = {"negative.txt":"11 failed", "focused.txt":"28 passed",
            "expanded.txt":"34 passed", "fixed.txt":"341 passed"}
for name, result in expected.items():
    assert result in (OUT/name).read_text(encoding="utf-8")
tap = (OUT/"client-tests.txt").read_text(encoding="utf-8")
assert re.search(r"^# pass 440$",tap,re.M) and re.search(r"^# skipped 0$",tap,re.M)
summary = dict(source_commit=SOURCE, source_tree=final["source_tree"],
    related_python_passed=341,client_passed=440,typecheck="passed",
    full_backend_rerun=False,client_evidence_mode="fresh_fixed_archive",
    typescript=client["typescript"],node=client["node"],temporary_scopes_removed=5,
    device_verified=False,multi_principal_retrieval_verified=False,
    acceptance_promoted=False,records=records,client_record=client,
    negative_probes=dict(python_failed=11,client_failed=3),
    log_sha256_lf={name:hashlib.sha256((OUT/name).read_bytes().replace(b"\r\n",b"\n")).hexdigest()
        for name in (*expected,"client-negative.txt","client-focused.txt","client-tests.txt","typecheck.txt")})
(OUT/"committed-evidence.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

path = ROOT/"docs/协作开发/workboard.json"
board = json.loads(path.read_text(encoding="utf-8"))
before = {r["id"]:tuple(r.get(k) for k in ("owner","reviewer","status")) for r in board["dispatches"]}
cards = ["ALN-16-Q","ALN-24-F","ALN-30-B"]
batch_id = "REVIEW-2026-10-26-EVIDENCE-SCOPE"
batch = dict(id=batch_id,date="2026-10-26",reviewer="codex-runtime",cards=cards,
    fixed_cards=cards,fix_commit=SOURCE,report=REPORT,evidence=EVIDENCE,
    status="fixes_delivered_pending_independent_recheck",product_acceptance_added=False,
    published_by_review=False,scope="九类证据/策略/客户端问题修复；固定相关341+客户端440/类型检查，不替代全量、设备或多主体消费验收")
board["code_review_batches"] = [r for r in board["code_review_batches"] if r["id"] != batch_id] + [batch]
notes = {
    "ALN-16-Q":"删除执行必需预览摘要/布尔参数，隐私开关显式校验，导出审计无1000窗口误拒，仓储读失败不冒称零残留。大导出/清理期、跨库删除原子性与设备仍待。",
    "ALN-24-F":"回退核对原legacy摘要和分区版本；迁移事务失败回滚、逐次追加回执、关闭分区不冒称迁移、actor取鉴权主体。真实多主体检索/上下文与UI消费仍未接。",
    "ALN-30-B":"恢复范围同步写回allow/deny；限制性allow与策略未加载状态如实显示。仅全局范围控件修复，项目/节点视图、全量下载、真机/模型上下文端到端仍待。"}
for row in board["dispatches"]:
    if row["id"] not in cards:
        continue
    row["latest_code_review"] = batch
    row["next_step"] = notes[row["id"]] + " 固定相关341过、客户端新归档440过+类型检查；本轮修复待其他审阅人复核，原review状态保留。"
    evidence = row.setdefault("evidence",[])
    evidence[:] = [r for r in evidence if not isinstance(r,dict) or r.get("log") != EVIDENCE]
    evidence.append(dict(date="2026-10-26",source_commit=SOURCE,source_tree=final["source_tree"],
        result=row["next_step"],log=EVIDENCE,delivery=REPORT))
if not any(r.get("source_commit") == SOURCE for r in board["post_candidate_changes"]):
    paths = subprocess.check_output(["git","diff-tree","--no-commit-id","--name-only","-r",SOURCE],
        cwd=ROOT,text=True,encoding="utf-8").splitlines()
    board["post_candidate_changes"].append(dict(id="discovered-"+SOURCE[:12],
        title="fix(review): 收紧证据删除与迁移回退并恢复范围开关语义",workpackage="ALN-16",
        source_commit=SOURCE,files=paths,status="source_registered_pending_validation",
        candidate_included=False,independent_review=False,report=REPORT,
        scope="16-Q/24-F/30-B修复固定相关341+客户端440；新增修复待其他审阅人复核，非整卡接受"))
for row in board["dispatches"]:
    assert tuple(row.get(k) for k in ("owner","reviewer","status")) == before[row["id"]]
board["updated_at"] = "2026-10-26"
path.write_text(json.dumps(board,ensure_ascii=False,indent=1)+"\n",encoding="utf-8")

for name in ("ALN-16-Q-聚合观测与受控导出删除API接线.md",
             "ALN-24-F-多主体记忆范围策略与迁移隔离.md",
             "ALN-30-B-客户端工作区成果与记忆控制接线.md"):
    path = ROOT/"docs/协作开发/交付"/name
    body = path.read_text(encoding="utf-8")
    if "2026-10-26 证据与策略接续复核" in body:
        continue
    heading, remainder = body.split("\n",1)
    body = (heading + "\n\n> 2026-10-26 证据与策略接续复核：" + notes[name[:8]]
        + " 修复093c561，固定相关341过、客户端新归档440过/类型检查；新增修复待其他审阅人复核。"
        + "下文为原交付历史描述，最新行为及保留缺口见[复核报告](../验收/2026-10-26-证据消费与记忆策略接续复核.md)，不扩大接受。\n"
        + remainder)
    body = body.replace("## 验证证据\n","## 验证证据（原交付历史记录）\n")
    path.write_text(body,encoding="utf-8")

path = ROOT/"docs/协作开发/待接需求_2026-10-26.md"
body = path.read_text(encoding="utf-8")
if "## 十三、证据消费与记忆策略接续复核" not in body:
    body += """

## 十三、证据消费与记忆策略接续复核

固定修复 `093c561`：相关后端341项通过，客户端全新归档440项通过、TypeScript5.9.3检查通过；不是全量后端、设备或真实多主体检索接受。九类问题与复现实验见[复核报告](验收/2026-10-26-证据消费与记忆策略接续复核.md)。5处新建临时范围已回收，原缓存/依赖和其他在途内容保留。

| 卡片 | 已修复 | 待接/验收缺口 |
|---|---|---|
| ALN-16-Q | 删除必需预览摘要、布尔参数与隐私开关校验；旧导出跨1000条审计仍可验真；仓储读失败不冒称零残留 | 大导出流式/保留期、管理端/设备；删除跨库非原子，需维护窗口；副作用/审批/审计按原执行器保留 |
| ALN-24-F | 回退核对原legacy摘要/分区版本，写事务及失败回滚、逐次迁移回执和鉴权actor | 分区策略→实际检索/上下文、真实多主体鉴权与principal-policy管理端；本轮不把组件隔离当全链隔离 |
| ALN-30-B | 范围禁用/恢复真正更新allow/deny；未加载/限制性allow如实显示 | 项目/工作区节点、全量成果下载、真机UI和模型上下文端到端 |

三卡原owner dsh-lead、reviewer待定和review状态保留；新修复需另一审阅人复核，父包不关闭。此前13-S/09-I/18-H/19-A修复及真实供应商费用持久化等仍按前节跟踪。
"""
    path.write_text(body,encoding="utf-8")
print("Registered 341 related Python + 440 fresh client/typecheck; 5 new scopes removed; ownership/status preserved")
