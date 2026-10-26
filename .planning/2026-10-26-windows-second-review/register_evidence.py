"""Register an independent Windows second review and pending resource fixes."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PREFIX = OUT.relative_to(ROOT).as_posix()
SOURCE = "5bc1725a52ec71ebef3ba9b8d98d03b56c76bc63"
REPORT = "docs/协作开发/验收/2026-10-26-Windows二审与资源恢复复核.md"
EVIDENCE = PREFIX + "/committed-evidence.json"


def git(*args):
    return subprocess.check_output(["git",*args],cwd=ROOT,text=True,encoding="utf-8").strip()


records = {p.name:json.loads(p.read_text(encoding="utf-8")) for p in OUT.glob("*-validation.json")}
matrix = json.loads((OUT/"platform-matrix.json").read_text(encoding="utf-8"))
environment = json.loads((OUT/"environment.json").read_text(encoding="utf-8"))
assert len(records) == 3 and all(r["temporary_data_removed"] for r in records.values())
assert matrix["all_variants_matched"] and matrix["isolated_files_restored"] and matrix["temporary_data_removed"]
assert len(matrix["variants"]) == 4 and environment["platform_system"] == "Windows"
fixed = records["fixed-validation.json"]
assert fixed["snapshot_commit"] == SOURCE and fixed["exit_code"] == 0
for name, count in (("resource-negative.txt","6 failed, 2 passed"),
                    ("resource-focused.txt","50 passed"),("fixed.txt","295 passed")):
    assert count in (OUT/name).read_text(encoding="utf-8")
for path in ("evoagent/release/manager.py","evoagent/release/delivery.py",
             "tests/test_release_signing.py","tests/test_windows_terminal_validation.py"):
    assert git("rev-parse",matrix["source_commit"]+":"+path) == git("rev-parse",SOURCE+":"+path)
logs = [p.name for p in OUT.glob("platform-*.txt")] + ["resource-negative.txt","resource-focused.txt","fixed.txt"]
summary = dict(source_commit=SOURCE,source_tree=fixed["source_tree"],related_python_passed=295,
    full_backend_rerun=False,client_rerun=False,device_verified=False,new_whole_host_stress_test=False,
    windows_second_review="accepted_for_pinned_windows_python_host_slice",
    resource_fixes="pending_independent_recheck",temporary_scopes_removed=4,
    records=records,platform_matrix=matrix,environment=environment,
    platform_source_blobs_unchanged_between_matrix_and_fixed_validation=True,
    log_sha256_lf={name:hashlib.sha256((OUT/name).read_bytes().replace(b"\r\n",b"\n")).hexdigest() for name in logs})
(OUT/"committed-evidence.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

path = ROOT/"docs/协作开发/workboard.json"
board = json.loads(path.read_text(encoding="utf-8"))
before = {r["id"]:tuple(r.get(k) for k in ("owner","reviewer","status")) for r in board["dispatches"]}
windows_id = "REVIEW-2026-10-26-WINDOWS-SECOND"
resource_id = "REVIEW-2026-10-26-RESOURCE-RECOVERY"
windows = dict(id=windows_id,date="2026-10-26",reviewer="codex-runtime",cards=["ALN-21-A"],
    reviewed_commit=SOURCE,report=REPORT,evidence=EVIDENCE,
    status="independent_second_review_accepted_windows_host_slice",published_by_review=False,
    scope="Windows Python宿主固定版本/回执组件/真实击杀恢复/组合恢复与signing因果矩阵；不扩大原生客户端/设备/生产接受")
resource = dict(id=resource_id,date="2026-10-26",reviewer="codex-runtime",cards=["ALN-15-D","ALN-25-B"],
    fixed_cards=["ALN-15-D","ALN-25-B"],fix_commit=SOURCE,report=REPORT,evidence=EVIDENCE,
    status="fixes_delivered_pending_independent_recheck",product_acceptance_added=False,
    published_by_review=False,scope="缺有效采样标记拒主动启动、无效采样不自动恢复/遗忘暂停集合；固定相关295，不代新25-C执行链验收")
board["code_review_batches"] = [r for r in board["code_review_batches"] if r["id"] not in (windows_id,resource_id)] + [windows,resource]
for row in board["dispatches"]:
    if row["id"] not in ("ALN-21-A","ALN-15-D","ALN-25-B"):
        continue
    if row["id"] == "ALN-21-A":
        if not any(r.get("report")==REPORT for r in row.get("review_history",[])):
            row.setdefault("review_history",[]).append(dict(date="2026-10-26",previous_status=row["status"],
                previous_reviewer=row.get("reviewer"),reason="另一审阅人完成固定Windows宿主切片二审；矩阵归因与真实击杀/恢复复验通过",report=REPORT))
        row["status"] = "accepted"
        row["reviewer"] = "codex-runtime"
        row["delivered_commit"] = git("rev-parse","dbd32a1^{commit}")
        row["second_review"] = windows
        row["latest_code_review"] = windows
        row["next_step"] = "Windows Python宿主切片二审通过：平台归因4变体与固定相关295过。CLI host-deploy/restore现已有入口；原生客户端/平板/Android联验/API管理面仍待，发布基线不变。依赖库新修复及25-C/15-E不因本卡二审自批。"
    else:
        if row["status"] == "accepted":
            row.setdefault("review_history",[]).append(dict(date="2026-10-26",previous_status="accepted",
                previous_reviewer=row.get("reviewer"),reason="二审发现采样有效性/恢复集合新缺陷，已修但新代码待另一审阅人复核",report=REPORT))
            row["status"] = "review"
        row["latest_code_review"] = resource
        row["next_step"] = "5bc1725修采样未知放行/无效采样自动恢复并清空暂停集合，固定相关295过，需他人复核。原0.768恢复指新准入不是0.70低水位resume；25-C新执行接线另审，不沿用本轮作为执行验收。"
        if row["id"] == "ALN-15-D" and not row.get("delivery"):
            row["delivery"] = "docs/协作开发/交付/ALN-15-D-设备资源准入与核心任务优先.md"
    evidence = row.setdefault("evidence",[])
    evidence[:] = [r for r in evidence if not isinstance(r,dict) or r.get("log") != EVIDENCE]
    evidence.append(dict(date="2026-10-26",source_commit=SOURCE,source_tree=fixed["source_tree"],
        result=row["next_step"],log=EVIDENCE,delivery=REPORT))
for row in board["dispatches"]:
    if row["id"] in ("ALN-15-D","ALN-25-B"):
        assert tuple(row.get(k) for k in ("owner","reviewer")) == before[row["id"]][:2]
    elif row["id"] == "ALN-21-A":
        assert row["owner"] == before[row["id"]][0]
    else:
        assert tuple(row.get(k) for k in ("owner","reviewer","status")) == before[row["id"]]
for ref,package,scope in ((SOURCE,"ALN-15","资源采样/恢复修复，295相关，待独立复核"),
    ("cefdc7c","ALN-15","并行15-E原生采样代码登记；本轮不复用作者447/Kotlin作为验收"),
    ("4c2a942","ALN-25","并行25-C实际提交接线登记；不在本轮5bc1725固定回归范围")):
    commit = git("rev-parse",ref+"^{commit}")
    if any(r.get("source_commit")==commit for r in board["post_candidate_changes"]):
        continue
    files = git("diff-tree","--no-commit-id","--name-only","-r",commit).splitlines()
    board["post_candidate_changes"].append(dict(id="discovered-"+commit[:12],
        title=git("show","-s","--format=%s",commit),workpackage=package,source_commit=commit,
        files=[p for p in files if p.startswith(("evoagent/","tests/","android-app/"))],
        status="source_registered_pending_validation",candidate_included=False,
        independent_review=False,report=REPORT,scope=scope))
board["updated_at"] = "2026-10-26"
path.write_text(json.dumps(board,ensure_ascii=False,indent=1)+"\n",encoding="utf-8")

headers = {
    "docs/协作开发/交付/ALN-21-A-Windows终端扩展验证.md": "Windows Python宿主切片二审通过；4变体签名归因与真实击杀/组合恢复复验成立。当前已有host-deploy/host-restore CLI入口，原‘无CLI’为历史状态；不扩大原生客户端/设备/生产接受。",
    "docs/协作开发/验收/2026-10-26-dsh复核-21A.md": "一审未完成的签名归因已由第二审阅人用归档/新进程复现；旧Windows断言在新平台表下1失败/8通过，当前Linux断言两种平台表均9通过，不以导入固化作为归因。下文为一审历史。",
    "docs/协作开发/交付/ALN-25-B-主动强度停止与资源非干扰.md": "二审确认两条采样未知处理缺陷，5bc1725修复后重新review，新修复待他人复核。原真实CPU饱和证据保留；0.768仅证明新启动再次准入，不是低水位resume。25-C现已提交实际入口，另审。",
    "docs/协作开发/验收/2026-10-26-dsh复核-25B-真实资源压力.md": "本CPU证据的recovered是may_start重新准入，不等同已暂停任务低水位恢复；第二审阅人另发现采样未知处理缺陷并修复，25-B重新review。下文保留一审观测。",
    "docs/协作开发/交付/ALN-15-D-设备资源准入与核心任务优先.md": "无效采样不再自动resume或清空暂停集合，5bc1725固定相关295过；前台既有fail-open准入保留。因新缺陷与修复待他人复核，重新review。"}
for relative,note in headers.items():
    path = ROOT/relative
    body = path.read_text(encoding="utf-8")
    if "2026-10-26 Windows二审与资源复核" in body:
        continue
    title,rest = body.split("\n",1)
    link = "../验收/2026-10-26-Windows二审与资源恢复复核.md" if "/交付/" in relative else "2026-10-26-Windows二审与资源恢复复核.md"
    path.write_text(title+"\n\n> 2026-10-26 Windows二审与资源复核："+note+" [复核报告]("+link+")。\n"+rest,encoding="utf-8")
path = ROOT/"docs/协作开发/待接需求_2026-10-26.md"
body = path.read_text(encoding="utf-8")
if "## 十六、Windows二审与资源恢复复核" not in body:
    body += """

## 十六、Windows二审与资源恢复复核

21-A的Windows Python宿主切片二审通过，owner不变，最终reviewer为codex-runtime，dsh一审保留。隔离四变体明确signing归因；固定5bc1725相关295项通过（含真实子进程击杀/恢复），不是全量后端或客户端重跑。详见[二审报告](验收/2026-10-26-Windows二审与资源恢复复核.md)。

15-D/25-B发现采样未知放行与自动恢复/丢失暂停集合两处问题，5bc1725已修，原accepted重新review；原owner/reviewer保留，新代码待另一审阅人复核。原真实CPU JSON保留为一审证据；0.768只证明新准入，不证明0.70低水位resume。本轮未再次制造整机饱和。

| 继续接手范围 | 状态与边界 |
|---|---|
| Windows后续 | host-deploy/restore CLI已存在；API/管理面、Windows原生客户端、平板/Android联验与生产存储仍待；不以本卡关闭父包或发布 |
| 15-D/25-B修复 | 无效样本保持暂停集合，恢复需有效低水位；缺sample_valid不允许主动启动。需独立复核本轮两处补丁 |
| 15-E | cefdc7c/a3e05d3已交付原生资源采样，本轮未重跑447客户端/tsc/Kotlin或设备压力；按该卡独立审核 |
| 25-C | 4c2a942/59ce8df已交付实际主动执行，不能再标成完全未开发；不在本轮5bc1725快照，需核对准入、预留/释放、权限/停止及真实资源消费 |

4处新受控临时范围均已回收，原缓存/依赖及其他在途内容保留。此前知识身份隔离、候选/发布等待接缺口继续按前节跟踪。
"""
    path.write_text(body,encoding="utf-8")
print("Windows second review accepted for pinned host slice; 15-D/25-B fixes pending; 295 related passes, 4 scopes removed")
