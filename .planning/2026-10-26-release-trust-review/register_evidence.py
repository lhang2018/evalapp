"""Record fixed source evidence while preserving concurrent task ownership."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PREFIX = OUT.relative_to(ROOT).as_posix()
SOURCE = "e1365ef857490936b4db6f5a6b1603059cc937cf"
REPORT = "docs/协作开发/验收/2026-10-26-发布撤销与回滚目标信任复核.md"
EVIDENCE = PREFIX + "/committed-evidence.json"
records = {name:json.loads((OUT/name).read_text(encoding="utf-8")) for name in
           ("negative-validation.json","focused-validation.json","fixed-validation.json")}
client = json.loads((OUT/"validation.json").read_text(encoding="utf-8"))
final = records["fixed-validation.json"]
assert final["snapshot_commit"] == SOURCE and final["exit_code"] == 0
assert all(row["temporary_data_removed"] for row in records.values())
assert client["commit"] == SOURCE and client["temporary_data_removed"]
assert client["dependency_junction_removed"] and client["installed_dependencies_preserved"]
assert client["results"] == {"client-tests":0,"typecheck":0}
assert "292 passed" in (OUT/"fixed.txt").read_text(encoding="utf-8")
assert "74 passed" in (OUT/"focused.txt").read_text(encoding="utf-8")
assert "8 failed, 11 passed" in (OUT/"negative.txt").read_text(encoding="utf-8")
tap = (OUT/"client-tests.txt").read_text(encoding="utf-8")
assert re.search(r"^# pass 437$",tap,re.M) and re.search(r"^# skipped 0$",tap,re.M)
tree = subprocess.check_output(["git","rev-parse",SOURCE+"^{tree}"],cwd=ROOT,text=True).strip()
assert tree == final["source_tree"] == client["source_tree"]
summary = dict(source_commit=SOURCE,source_tree=tree,python_passed=292,client_passed=437,typecheck="passed",
    typescript=client["typescript"],node=client["node"],full_backend_rerun=False,client_evidence_mode="fresh_fixed_archive",
    initial_result=dict(failed=8,passed=11,scope="9 new release probes plus 10 previous restore regressions"),
    focused_passed=74,records=records,client_record=client,temporary_data_removed=True,
    product_acceptance_added=False,device_verified=False,
    log_sha256_lf={name:hashlib.sha256((OUT/name).read_bytes().replace(b"\r\n",b"\n")).hexdigest()
                   for name in ("negative.txt","focused.txt","fixed.txt","client-tests.txt","typecheck.txt")})
(OUT/"committed-evidence.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
path = ROOT/"docs/协作开发/workboard.json"
board = json.loads(path.read_text(encoding="utf-8"))
before = {row["id"]:tuple(row.get(key) for key in ("owner","reviewer","status")) for row in board["dispatches"]}
id = "REVIEW-2026-10-26-RELEASE-TRUST"
batch = dict(id=id,date="2026-10-26",reviewer="codex-runtime",cards=["ALN-03-P","ALN-11-P"],
    fixed_cards=["ALN-03-P","ALN-11-P"],fix_commit=SOURCE,report=REPORT,evidence=EVIDENCE,
    status="fixes_delivered_pending_independent_recheck",product_acceptance_added=False,published_by_review=False,
    scope="撤销公钥端侧投影、回滚实际目标信任与归因；恢复10项接续通过，不是全量或设备接受")
if not any(row["id"]==id for row in board["code_review_batches"]):board["code_review_batches"].append(batch)
for card in board["dispatches"]:
    if card["id"] not in batch["cards"]:continue
    if not any(isinstance(row,dict) and row.get("log")==EVIDENCE for row in card["evidence"]):
        card["evidence"].append(dict(date="2026-10-26",source_commit=SOURCE,source_tree=tree,
            result="撤销公钥保留revoked、实际回滚目标校验/信任字段统一；固定Python相关292过、客户端437过+类型检查；新增修复待复核，不扩大原接受",log=EVIDENCE,delivery=REPORT))
    card["latest_code_review"] = batch
    if card["id"] == "ALN-03-P":
        card["next_step"]="原签发/联验切片接受保留；e1365ef修完整公钥导出丢失revoked，真实端侧验签回归通过。固定相关292+客户端437/类型检查；新增修复待复核，生产信任根/撤销同步和真机仍保留。"
    else:
        card["next_step"]="原安装退役切片接受保留；e1365ef为rolled_back解析实际目标并校验退役/生产身份，回执密钥/摘要/版本归因一致，源撤销仍可退回可信目标。固定相关292+客户端437/类型检查；新增修复待复核，生产/设备接受另验。"
change_id="discovered-"+SOURCE[:12]
if not any(row["id"]==change_id for row in board["post_candidate_changes"]):
    board["post_candidate_changes"].append(dict(id=change_id,title="fix(release): 保留撤销标记并校验实际回滚目标信任",workpackage="ALN-03",source_commit=SOURCE,
        status="source_registered_pending_validation",files=["evoagent/release/delivery.py","evoagent/release/identity.py","tests/test_release_trust_review_20261026.py"],
        candidate_included=False,independent_review=False,report=REPORT,scope="03-P/11-P信任修复；292相关+437客户端新跑，新增修复待复核，非全量/生产发布"))
# Concurrent code is registered as pending, without promoting its task or
# attributing its separate functional verification to this trust review.
for ref, package in (("1b590a3","ALN-30"),("2e0c857","ALN-24"),("aa19558","ALN-16")):
    commit=subprocess.check_output(["git","rev-parse",ref+"^{commit}"],cwd=ROOT,text=True).strip()
    if any(row.get("source_commit")==commit for row in board["post_candidate_changes"]):continue
    paths=subprocess.check_output(["git","diff-tree","--no-commit-id","--name-only","-r",commit],cwd=ROOT,text=True).splitlines()
    paths=[path for path in paths if path.startswith(("evoagent/","android-app/","tests/"))]
    board["post_candidate_changes"].append(dict(id="discovered-"+commit[:12],
        title=subprocess.check_output(["git","show","-s","--format=%s",commit],cwd=ROOT,text=True,encoding="utf-8").strip(),
        workpackage=package,source_commit=commit,status="source_registered_pending_validation",files=paths,
        candidate_included=False,independent_review=False,report=REPORT,
        scope="并行交付源码补登记，不改原认领/审阅；具体功能按各卡验收，16-Q不在本轮e136固定回归范围"))
for card in board["dispatches"]:
    assert tuple(card.get(key) for key in ("owner","reviewer","status")) == before[card["id"]]
board["updated_at"]="2026-10-26"
path.write_text(json.dumps(board,ensure_ascii=False,indent=1)+"\n",encoding="utf-8")
print("Recorded 292 targeted Python + 437 fresh client/typecheck; concurrent ownership preserved")
