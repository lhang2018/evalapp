"""Register exact tested trees without promoting task acceptance."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PREFIX = OUT.relative_to(ROOT).as_posix()
REPORT = "docs/协作开发/验收/2026-10-26-新增宿主与音视频费用复核.md"
EVIDENCE = PREFIX + "/committed-evidence.json"


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, encoding="utf-8").strip()


source = git("rev-parse", "01766f5^{commit}")
host_source = git("rev-parse", "0ba37e1^{commit}")
tree = git("rev-parse", source + "^{tree}")
records = {p.name: json.loads(p.read_text(encoding="utf-8"))
           for p in OUT.glob("*-validation.json")}
assert len(records) == 8
assert all(row["temporary_data_removed"] for row in records.values())
assert records["combined-validation.json"]["exit_code"] == 0
# The combined command requested HEAD; bind its captured archive tree to the
# immutable commit, rather than pretending HEAD is an immutable evidence label.
assert records["combined-validation.json"]["source_tree"] == tree
assert records["fixed-validation.json"]["source_tree"] == git("rev-parse", host_source + "^{tree}")
expected = {"negative.txt": "9 failed", "focused.txt": "27 passed",
            "positive.txt": "10 passed", "fixed.txt": "340 passed",
            "media-negative.txt": "10 failed, 1 passed",
            "media-focused.txt": "17 passed", "media-final-focused.txt": "18 passed",
            "combined.txt": "358 passed"}
for name, result in expected.items():
    assert result in (OUT / name).read_text(encoding="utf-8")
summary = dict(source_commit=source, source_tree=tree, host_fix_commit=host_source,
    related_python_passed=358, full_backend_rerun=False, client_rerun=False,
    real_vendor_verified=False, device_verified=False, acceptance_promoted=False,
    temporary_scopes_removed=len(records), records=records,
    combined_request_snapshot="HEAD; captured tree verified against exact source_commit",
    log_sha256_lf={name: hashlib.sha256((OUT/name).read_bytes().replace(b"\r\n",b"\n")).hexdigest()
                   for name in expected})
(OUT/"committed-evidence.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n", encoding="utf-8")

path = ROOT / "docs/协作开发/workboard.json"
board = json.loads(path.read_text(encoding="utf-8"))
before = {row["id"]: tuple(row.get(k) for k in ("owner","reviewer","status"))
          for row in board["dispatches"]}
batch_id = "REVIEW-2026-10-26-NEW-HOST-MEDIA"
cards = ["ALN-13-S","ALN-09-I","ALN-18-H","ALN-19-A"]
batch = dict(id=batch_id,date="2026-10-26",reviewer="codex-runtime",cards=cards,
    fix_commit=source,fix_commits=[host_source,source],report=REPORT,evidence=EVIDENCE,
    status="fixes_delivered_pending_independent_recheck",product_acceptance_added=False,
    published_by_review=False,scope="四卡七类问题修复；固定相关358过；非全量/真机/生产接受")
board["code_review_batches"] = [r for r in board["code_review_batches"] if r["id"] != batch_id] + [batch]
notes = {
    "ALN-13-S":"修正stop超时线程丢失/重复启动，running与stopping如实显示；周期与超时有限值校验。生产长跑、多进程和资源治理联调仍待。",
    "ALN-09-I":"修正release_before取值时序；评测期间独立发布如实显示差异。本轮未建立candidate_id/制品及数据集仓库权威绑定，不当发布授权。",
    "ALN-18-H":"项目工具表共享宿主当前权限控制器/存储，deny/未确认拒绝改写、明确确认可执行。任务授权/专家自动选择、真实项目与升级恢复仍待。",
    "ALN-19-A":"拒绝非有限配置/非法实结费用/无产物标识；失败费用unknown保留预留，明确账单对账后恢复，不冒称退款。费用仅单实例内存，全局预算/持久账本、真实供应商仍待。"}
for row in board["dispatches"]:
    if row["id"] not in cards:
        continue
    row["latest_code_review"] = batch
    row["next_step"] = notes[row["id"]] + " 固定相关358过；本轮修复待其他审阅人复核，原review状态保留。"
    row.setdefault("evidence",[]).append(dict(date="2026-10-26",source_commit=source,
        source_tree=tree,result=row["next_step"],log=EVIDENCE,delivery=REPORT))

for ref, package in (("0bf8e05","ALN-13"),("1d758e9","ALN-09"),
    ("f43c8ef","ALN-18"),("f772860","ALN-19"),(host_source,"ALN-30"),(source,"ALN-19")):
    commit = git("rev-parse", ref + "^{commit}")
    if any(row.get("source_commit") == commit for row in board["post_candidate_changes"]):
        continue
    paths = git("diff-tree","--no-commit-id","--name-only","-r",commit).splitlines()
    paths = [p for p in paths if p.startswith(("evoagent/","tests/","android-app/"))]
    board["post_candidate_changes"].append(dict(id="discovered-"+commit[:12],
        title=git("show","-s","--format=%s",commit),workpackage=package,source_commit=commit,
        files=paths,status="source_registered_pending_validation",candidate_included=False,
        independent_review=False,report=REPORT,scope="原卡/修复有固定相关回归；本轮不自批修复或整卡/父包接受"))
for row in board["dispatches"]:
    assert tuple(row.get(k) for k in ("owner","reviewer","status")) == before[row["id"]]
board["updated_at"] = "2026-10-26"
path.write_text(json.dumps(board,ensure_ascii=False,indent=1)+"\n",encoding="utf-8")

deliveries = [
    "ALN-13-S-长期目标调度启动与持久周期消费.md",
    "ALN-09-I-真实候选宿主消费与队列管理入口.md",
    "ALN-18-H-编程专家按项目安装与真实工具消费.md",
    "ALN-19-A-真实音视频供应商切片.md"]
for name in deliveries:
    path = ROOT/"docs/协作开发/交付"/name
    body = path.read_text(encoding="utf-8")
    heading, remainder = body.split("\n",1)
    card = name[:8]
    note = ("\n> 2026-10-26 接续审查：" + notes[card] +
        " 本轮固定相关358项通过，修复提交0ba37e1/01766f5，新增修复待其他审阅人复核。"
        "下文修改前后及首轮验证是历史记录，最新行为以[复核报告](../验收/2026-10-26-新增宿主与音视频费用复核.md)为准；不提升整卡或生产接受。\n")
    if "2026-10-26 接续审查" not in body:
        body = heading + "\n" + note + remainder
        body = body.replace("## 验证证据\n", "## 验证证据（首轮历史记录）\n")
        path.write_text(body,encoding="utf-8")

waiting = ROOT/"docs/协作开发/待接需求_2026-10-26.md"
body = waiting.read_text(encoding="utf-8")
if "## 十二、最新宿主与音视频组件复核" not in body:
    body += """

## 十二、最新宿主与音视频组件复核

固定源码 `01766f5`（含宿主修复 `0ba37e1`），相关358项通过；非后端全量、客户端新跑或真实供应商/设备验收。七个确认问题及验证见[复核报告](验收/2026-10-26-新增宿主与音视频费用复核.md)。原负责人 dsh-lead、reviewer 待定、四卡 review 状态保留；新增修复待其他审阅人复核。

| 卡片 | 已修复 | 下一步缺口 |
|---|---|---|
| ALN-13-S | 停止超时保留工作线程、排空前不重复启动、启停有限值校验 | 生产长跑/自动拉起、多进程压力、资源治理联调 |
| ALN-09-I | 发布版本在真实评测前后分别采样 | candidate_id→制品与数据集仓库权威绑定；CLI/App 管理面与生产并发。标签不能当发布授权 |
| ALN-18-H | 独立项目工具表共享宿主权限，拒绝/确认/双根隔离实测 | 任务授权与自动选专家接线；真实项目/固定依赖、升级和半写入恢复 |
| ALN-19-A | 非有限费用拒绝、未知费用预留/对账、空产物标识拒绝 | 进程内记录需接既有持久费用/副作用权威；真实供应商协议/凭据/预算/实际产物与账单。人工账单登记不代表自动验真或退款 |

本轮只清理8处新建受控测试临时目录，其他在途代码/缓存未混入提交；不复用作者全量日志作为本轮通过证据。既有30-B/24-F/16-Q仍按各卡独立审阅。
"""
    waiting.write_text(body,encoding="utf-8")
print("Registered 358 related passes, 8 removed scopes; task ownership/status preserved.")
