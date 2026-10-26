"""Bind two fixed source slices and register corrections without self-acceptance."""
import hashlib
import json
from pathlib import Path
import subprocess

root=Path(__file__).resolve().parents[2]
folder=Path(__file__).resolve().parent
backend="7b16edca06bc633aa2b7806a417c7a11568bf82b"
client="369981e0382a7c895a268dc6b4c4b6a194a574c1"
report="docs/协作开发/验收/2026-10-27-资源准入与手机下发边界复核.md"
evidence=".planning/2026-10-26-runtime-admission-review/committed-evidence.json"
def git(*args):
    return subprocess.check_output(["git",*args],cwd=root,text=True,encoding="utf-8").strip()
records={}
for name in ("negative-validation","json-negative-validation","focused-validation","committed-validation","initial-validation","validation"):
    value=json.loads((folder/(name+".json")).read_text(encoding="utf-8"))
    owned=Path(value["owned_temp_directory"])
    assert owned.resolve().parent==(root/"outputs").resolve()
    assert value["temporary_data_removed"] and not owned.exists()
    records[name]=value
assert records["committed-validation"]["snapshot_commit"]==backend and records["committed-validation"]["exit_code"]==0
assert records["validation"]["commit"]==client and not any(records["validation"]["results"].values())
trees={}
for name in ("evoagent","tests"):
    trees[name]=git("rev-parse",backend+":"+name)
    assert trees[name]==git("rev-parse",client+":"+name)
changed=git("diff","--name-only",backend,client).splitlines()
assert changed and all(name.startswith("android-app/") for name in changed)
record={"date":"2026-10-27","started_date":"2026-10-26","reviewer":"codex-runtime",
    "fix_commits":[backend,client],"backend_source_commit":backend,"client_source_commit":client,
    "backend_source_tree":records["committed-validation"]["source_tree"],"client_source_tree":records["validation"]["source_tree"],
    "backend_subtree_identity_to_final_client_commit":trees,"intermediate_diff_only_android_app":changed,
    "report":report,"validation_records":records,"owned_validation_scopes_removed":len(records),
    "results":{"backend_negative":"20 failed, 1 passed; extra JSON probes 3 failed",
        "backend_focused":"74 passed","backend_fixed_related":"343 passed, 1 deselected",
        "client_negative":"8 failed","client_focused":"85 passed; final new boundary probes 10 passed",
        "initial_client":"459 passed","final_client":"469 passed, TypeScript exit 0"},
    "initial_fixture_typecheck_error":"TS2352 on intentionally invalid guard cast; changed via unknown; not a production defect",
    "not_run":["whole-host CPU saturation","full backend","Kotlin compiler","fixed APK/device/native UI thread","real provider/production"],
    "status":"fixes_delivered_pending_independent_recheck","independent_acceptance_added":False,
    "old_caches_dependencies_foreign_work_preserved":True,
    "log_sha256_lf":{p.name:hashlib.sha256(p.read_bytes().replace(b"\r\n",b"\n")).hexdigest() for p in folder.glob("*.txt")}}
(folder/"committed-evidence.json").write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
board_file=root/"docs/协作开发/workboard.json"
board=json.loads(board_file.read_text(encoding="utf-8"))
for card in board["dispatches"]:
    if card["id"] not in {"ALN-12-E","ALN-15-D"}: continue
    is_device=card["id"]=="ALN-12-E"
    source=client if is_device else backend
    scope=("前台等待窗口下发闸门与派发true/false/unknown诚实性；原生UI线程/固定APK仍待" if is_device else
           "控制器直接admit旁路、无效后台采样、未知启动决策/活动登记与严格JSON；前台兼容保留")
    result=("最终固定客户端469/TS通过；10新设备边界，非真机；新修复待独立确认" if is_device else
            "固定相关后端343/1整机饱和未跑；24新探针；旧观察已确认为真实旁路并修复，非生产压力接受")
    gaps=("原生桥/UI线程、固定APK/JS、多步骤/服务端规划、文件成果/人工裁决和服务端成果核验仍待" if is_device else
          "默认真实sampler/阈值/模型池/GPU与设备标定、运行中pause-stop消费仍待")
    assert card["status"]=="review"
    entry={"date":"2026-10-27","reviewer":"codex-runtime","source_commit":source,
           "report":report,"log":evidence,"scope":scope,"result":result}
    if entry not in card["evidence"]: card["evidence"].append(entry)
    if card.get("review_verdict")!="changes_fixed_pending_independent_recheck":
        card.setdefault("review_history",[]).append({"date":"2026-10-27","previous_review_verdict":card.get("review_verdict"),
            "reason":"接续发现新边界缺陷并修复，旧一审数字保留，新补丁待独立确认","report":report})
    card["review_verdict"]="changes_fixed_pending_independent_recheck"
    card["technical_review"]={"date":"2026-10-27","reviewer":"codex-runtime","source_commit":source,
        "status":"changes_fixed_pending_independent_recheck","report":report,"evidence":evidence,
        "independent_acceptance_added":False,"scope":scope}
    card["blocker"]="本轮新补丁待另一审阅人确认；"+gaps
    card["next_step"]=result+"；保留原owner/reviewer/一审人；"+gaps+"；"+report
for source,title,workpackage in (
    (backend,"fix(runtime): 无效资源采样暂缓后台并防御准入旁路","ALN-15"),
    (client,"fix(device): 下发前重验前台条件并如实记录派发事实","ALN-12")):
    entry={"id":"review-fix-"+source[:12],"title":title,"workpackage":workpackage,"source_commit":source,
        "files":git("diff-tree","--no-commit-id","--name-only","-r",source).splitlines(),
        "status":"source_registered_pending_validation","candidate_included":False,"independent_review":False,
        "report":report,"scope":"资源/手机消费边界接续修复；343相关/469客户端/TS，待独立接受"}
    if not any(c["id"]==entry["id"] for c in board["post_candidate_changes"]):board["post_candidate_changes"].append(entry)
batch={"id":"REVIEW-2026-10-27-ADMISSION-DEVICE","date":"2026-10-27","reviewer":"codex-runtime",
    "cards":["ALN-15-D","ALN-12-E"],"fixed_cards":["ALN-15-D","ALN-12-E"],"fix_commit":client,
    "fix_commits":[backend,client],"report":report,"evidence":evidence,
    "status":"fixes_delivered_pending_independent_recheck","product_acceptance_added":False,"published_by_review":False,
    "scope":"直接资源准入消费者与异步前台下发；原一审观察升级确认缺陷，保留原历史/审核分工；非设备/生产接受"}
if not any(c["id"]==batch["id"] for c in board["code_review_batches"]):board["code_review_batches"].append(batch)
board_file.write_text(json.dumps(board,ensure_ascii=False,indent=1)+"\n",encoding="utf-8")
print("registered two-card fixes and six cleaned validation scopes; owners/reviewers retained")
