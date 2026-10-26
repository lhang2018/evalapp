"""Record actual fixes and evidence without promoting historical acceptance."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PREFIX = OUT.relative_to(ROOT).as_posix()
SOURCE = "621a1914ada49647c99e65952f73b7fe29014c67"
REPORT = "docs/协作开发/验收/2026-10-26-他人验收台账与恢复入口代码复核.md"
EVIDENCE = PREFIX + "/committed-evidence.json"
records = {name:json.loads((OUT/name).read_text(encoding="utf-8")) for name in
           ("negative-validation.json","focused-validation.json","fixed-validation.json")}
final = records["fixed-validation.json"]
assert final["snapshot_commit"] == SOURCE and final["exit_code"] == 0
assert all(row["temporary_data_removed"] for row in records.values())
assert "291 passed" in (OUT/"fixed.txt").read_text(encoding="utf-8")
tree = subprocess.check_output(["git","rev-parse",SOURCE+"^{tree}"],cwd=ROOT,text=True).strip()
assert tree == final["source_tree"]
inventory = json.loads((OUT/"ledger-inventory.json").read_text(encoding="utf-8"))
assert inventory["cards"] == 105 and inventory["accepted_count"] == 75
assert not inventory["missing_document_refs"] and not inventory["unresolved_commit_refs"]
summary = dict(source_commit=SOURCE,source_tree=tree,passed=291,scope="restore/deployment/identity/control/runtime/ledger/tools; not full backend",
    initial_negative=dict(failed=7,passed=1),focused_passed=60,records=records,
    ledger_cards=105,accepted_metadata_checked=75,missing_standard_delivered_commit=29,
    client_rerun=False,full_backend_rerun=False,device_verified=False,product_acceptance_added=False,
    temporary_data_removed=True,
    log_sha256_lf={name:hashlib.sha256((OUT/name).read_bytes().replace(b"\r\n",b"\n")).hexdigest()
                   for name in ("negative.txt","focused.txt","fixed.txt")})
(OUT/"committed-evidence.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
path = ROOT/"docs/协作开发/workboard.json"
board = json.loads(path.read_text(encoding="utf-8"))
original = json.loads(subprocess.check_output(["git","show",SOURCE+":docs/协作开发/workboard.json"],cwd=ROOT).decode("utf-8"))
id = "REVIEW-2026-10-26-LEDGER-RESTORE"
batch = dict(id=id,date="2026-10-26",reviewer="codex-runtime",
    cards=["ALN-30-A","ALN-17-A","ALN-17-P","ALN-11-P"],fixed_cards=["ALN-30-A","ALN-17-A"],
    read_only_recheck_cards=["ALN-17-P","ALN-11-P"],fix_commit=SOURCE,report=REPORT,evidence=EVIDENCE,
    status="fixes_delivered_pending_independent_recheck",product_acceptance_added=False,published_by_review=False,
    scope="105卡元数据普查；30-A恢复入口/17-A恢复门禁修复，17-P/11-P仅相关范围，不是整包重新验收")
if not any(row["id"]==id for row in board["code_review_batches"]):
    board["code_review_batches"].append(batch)
for card in board["dispatches"]:
    if card["id"] not in batch["fixed_cards"]:
        continue
    if not any(isinstance(row,dict) and row.get("log")==EVIDENCE for row in card["evidence"]):
        card["evidence"].append(dict(date="2026-10-26",source_commit=SOURCE,source_tree=tree,
            result="恢复入口三问题修复：全账本门禁/窗口外任务/真实失败回退；固定相关291过，新增修复待复核，不扩大旧接受",log=EVIDENCE,delivery=REPORT))
    card["latest_code_review"] = batch
    if card["id"] == "ALN-30-A":
        card["next_step"] = "30-A仍review；621a191修复host-restore未结束任务漏拦截、旧门禁1000窗口和失败未回退，固定相关291过，新修复待独立复核。旧2299/1日志仍缺精确树绑定，非当前全量；原拆分消费者与生产条件继续。"
    else:
        card["next_step"] = "原17-A切片接受保留；621a191补CLI/API恢复门禁全账本查询，新修复固定相关291过待复核。维护窗口/跨进程互斥及生产恢复按17包保留，不宣称整包关闭。"
changes = board["post_candidate_changes"]
change_id = "discovered-" + SOURCE[:12]
if not any(row["id"]==change_id for row in changes):
    changes.append(dict(id=change_id,title="fix(recovery): 恢复前查全账本并启用失败回退",workpackage="ALN-17",source_commit=SOURCE,
        status="source_registered_pending_validation",files=["evoagent/api/server.py","evoagent/cli.py","evoagent/core/persistence.py","tests/test_restore_entry_review_20261026.py"],
        candidate_included=False,independent_review=False,report=REPORT,scope="30-A/17-A恢复入口与门禁修复，291定向过；新增修复待复核，非全量/生产接受"))
for old in original["dispatches"]:
    now = next(row for row in board["dispatches"] if row["id"]==old["id"])
    assert all(now.get(key)==old.get(key) for key in ("owner","reviewer","status"))
board["updated_at"]="2026-10-26"
path.write_text(json.dumps(board,ensure_ascii=False,indent=1)+"\n",encoding="utf-8")
print("Registered 291 targeted results; existing owners/reviewers/statuses preserved")
