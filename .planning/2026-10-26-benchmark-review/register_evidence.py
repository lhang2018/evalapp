"""Preserve failed and successful archives and reopen affected accepted slices."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PREFIX = OUT.relative_to(ROOT).as_posix()
FIX = "fde8f899d8af9db6b1e4285b5ff75e83b51d7271"
REPORT = "docs/协作开发/验收/2026-10-26-候选基准与隔离文件边界复核.md"
EVIDENCE = PREFIX + "/committed-evidence.json"


def git(*args):
    return subprocess.check_output(["git",*args],cwd=ROOT,text=True,encoding="utf-8").strip()


records = {p.name:json.loads(p.read_text(encoding="utf-8")) for p in OUT.glob("*-validation.json")}
assert len(records) == 7
assert all(row["temporary_data_removed"] for row in records.values())
initial = records["fixed-validation.json"]
assert initial["snapshot_commit"] == FIX and initial["exit_code"] == 1
assert "1 failed, 310 passed" in (OUT/"fixed.txt").read_text(encoding="utf-8")
final = records["final-validation.json"]
assert final["exit_code"] == 0
source = final["snapshot_commit"]
assert re.fullmatch(r"[a-f0-9]{40}",source)
assert final["source_tree"] == git("rev-parse",source+"^{tree}")
paths = git("diff-tree","--no-commit-id","--name-only","-r",FIX).splitlines()
for path in paths:
    assert git("rev-parse",FIX+":"+path) == git("rev-parse",source+":"+path)
assert git("rev-parse",FIX+":android-app") == git("rev-parse",source+":android-app")
log = (OUT/"final.txt").read_text(encoding="utf-8")
match = re.search(r"(\d+) passed in",log)
assert match
passed = int(match[1])
summary = dict(fix_commit=FIX,source_commit=source,source_tree=final["source_tree"],
    related_python_passed=passed,full_backend_rerun=False,client_rerun=False,
    benchmark_version="host-bench/2",device_verified=False,production_acceptance_added=False,
    initial_fixed_result=dict(failed=1,passed=310,reason="27-C persisted knowledge invalidated an older in-memory assertion"),
    temporary_scopes_removed=8,recorded_validation_scopes=7,records=records,
    failed_archive_selection=dict(log=PREFIX+"/archive-selection-error.txt",requested="85de8e6a",
        pytest_executed=False,owned_temp_directory=str(ROOT/"outputs/review5-6kx1gipa"),
        temporary_data_removed=not (ROOT/"outputs/review5-6kx1gipa").exists()),
    benchmark_fix_blobs_unchanged_in_final_archive=True,
    log_sha256_lf={name:hashlib.sha256((OUT/name).read_bytes().replace(b"\r\n",b"\n")).hexdigest()
        for name in ("negative.txt","extended-negative.txt","descriptor-negative.txt","focused.txt","expanded.txt","fixed.txt","final.txt","archive-selection-error.txt")})
assert summary["failed_archive_selection"]["temporary_data_removed"]
(OUT/"committed-evidence.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
notes = {
    "ALN-09-H":"已复现rename/replace跨沙箱移动/覆盖合成宿主文件，fde8f89拦截两端路径并拒绝无法核验的描述符修改。目录内移动保留；原接受需重新复核，仍不是OS安全隔离/跨平台完整验收。",
    "ALN-10-E":"必跑崩溃遗漏、非布尔ok、唯一/类型清单、实际超时和发布后监测完整性已修；host-bench/2区分语义。原接受需重新复核，生产分布/独立oracle/发布门禁消费仍待。",
    "ALN-09-I":"实际基准消费已拒绝重复ID/非法required/domain，复用host-bench/2。candidate_id/dataset权威绑定、生产并发和CLI/App仍待；不自动发布。"}
path = ROOT/"docs/协作开发/workboard.json"
board = json.loads(path.read_text(encoding="utf-8"))
before = {r["id"]:tuple(r.get(k) for k in ("owner","reviewer","status")) for r in board["dispatches"]}
batch_id = "REVIEW-2026-10-26-BENCHMARK-SANDBOX"
batch = dict(id=batch_id,date="2026-10-26",reviewer="codex-runtime",cards=list(notes),
    fixed_cards=list(notes),fix_commit=FIX,validation_commit=source,report=REPORT,evidence=EVIDENCE,
    status="fixes_delivered_pending_independent_recheck",product_acceptance_added=False,
    published_by_review=False,scope=f"六类基准/隔离缺陷修复，固定相关{passed}过；首轮310过/1旧知识断言失败保留；非全量、设备或生产接受")
board["code_review_batches"] = [r for r in board["code_review_batches"] if r["id"] != batch_id] + [batch]
for row in board["dispatches"]:
    if row["id"] not in notes:
        continue
    if row["id"] in ("ALN-09-H","ALN-10-E") and row["status"] == "accepted":
        row.setdefault("review_history",[]).append(dict(date="2026-10-26",previous_status="accepted",
            previous_reviewer=row.get("reviewer"),reason="接续负向探针确认原核心边界缺陷；已修复但新代码待其他审阅人复核",report=REPORT))
        row["status"] = "review"
    row["latest_code_review"] = batch
    row["next_step"] = notes[row["id"]] + f" 固定相关{passed}过，新修复待其他审阅人复核。"
    evidence = row.setdefault("evidence",[])
    evidence[:] = [r for r in evidence if not isinstance(r,dict) or r.get("log") != EVIDENCE]
    evidence.append(dict(date="2026-10-26",source_commit=source,functional_commit=FIX,
        source_tree=final["source_tree"],result=row["next_step"],log=EVIDENCE,delivery=REPORT))
for row in board["dispatches"]:
    if row["id"] in ("ALN-09-H","ALN-10-E"):
        assert tuple(row.get(k) for k in ("owner","reviewer")) == before[row["id"]][:2]
    else:
        assert tuple(row.get(k) for k in ("owner","reviewer","status")) == before[row["id"]]
for ref, package, scope in ((FIX,"ALN-09","09-H/10-E/09-I基准修复；新修复待独立复核"),
    ("79cc06b","ALN-27","并行知识持久化源码登记，不代表本轮验收该卡"),
    ("85de8e6","ALN-27","知识持久化取代内存语义后的旧装配断言修正，固定318回归，不代表27-C功能接受")):
    commit = git("rev-parse",ref+"^{commit}")
    if any(row.get("source_commit") == commit for row in board["post_candidate_changes"]):
        continue
    files = git("diff-tree","--no-commit-id","--name-only","-r",commit).splitlines()
    board["post_candidate_changes"].append(dict(id="discovered-"+commit[:12],
        title=git("show","-s","--format=%s",commit),workpackage=package,source_commit=commit,
        files=[p for p in files if p.startswith(("evoagent/","tests/","android-app/"))],
        status="source_registered_pending_validation",candidate_included=False,
        independent_review=False,report=REPORT,scope=scope))
board["updated_at"] = "2026-10-26"
path.write_text(json.dumps(board,ensure_ascii=False,indent=1)+"\n",encoding="utf-8")

for name, card in (("ALN-09-H-真实候选宿主、持久问题队列与进程隔离.md","ALN-09-H"),
    ("ALN-10-E-真实候选收益与依赖回归证据.md","ALN-10-E"),
    ("ALN-09-I-真实候选宿主消费与队列管理入口.md","ALN-09-I")):
    path = ROOT/"docs/协作开发/交付"/name
    body = path.read_text(encoding="utf-8")
    if "2026-10-26 基准与隔离继续审查" in body:
        continue
    heading, rest = body.split("\n",1)
    body = (heading+"\n\n> 2026-10-26 基准与隔离继续审查："+notes[card]
        +f" 固定相关{passed}项通过；首轮310通过/1条旧知识断言失败及后续复验均保留。"
        +"见[复核报告](../验收/2026-10-26-候选基准与隔离文件边界复核.md)。原接受不扩展到本轮新修复；下文为原切片历史证据。\n"+rest)
    path.write_text(body,encoding="utf-8")
path = ROOT/"docs/协作开发/待接需求_2026-10-26.md"
body = path.read_text(encoding="utf-8")
if "## 十四、真实候选基准与文件边界继续审查" not in body:
    body += f"""

## 十四、真实候选基准与文件边界继续审查

修复fde8f89，基准升级host-bench/2。六类确认问题见[报告](验收/2026-10-26-候选基准与隔离文件边界复核.md)：rename/replace越界、必跑崩溃漏记、非布尔ok、歧义用例清单、超时声明不一致、无效发布后监测冒称闭环。

09-H/10-E由accepted重新进入review，原owner/reviewer保留；09-I仍review，新修复待其他审阅人复核。固定相关{passed}项通过；首轮310通过/1条并行27-C改变持久化语义而遗留的旧断言失败已保留，修正提交后复验。7处有验证记录的临时范围及1处SHA误写造成的归档启动失败范围均回收，不清旧缓存/其他在途内容；不是全量后端，客户端未重跑。

| 待接范围 | 后续工作 |
|---|---|
| 候选隔离 | OS级隔离、资源/输出内存上限、跨平台描述符集成；Python钩子不当强安全边界 |
| 基准/收益 | 生产数据集、独立结果oracle、真实监测样本与来源验证、发布门禁自动消费；旧v1证据不自动升级 |
| 09-I宿主 | candidate_id/dataset精确权威绑定、生产并发、CLI/App管理面 |
| 并行交付 | 27-C知识持久化及30-A/25-B独立一审按原卡继续验收，不因本轮基准回归直接关闭 |
"""
    path.write_text(body,encoding="utf-8")
print(f"Registered fixed {source[:7]}: {passed} related passes; 09-H/10-E reopened, other task states preserved")
