"""Register recovery outcome source/evidence, preserving all review assignments."""
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
source = "33c22a1e121539a056ef46f58fa5f545803d97b8"
report = "docs/协作开发/验收/2026-10-27-恢复裁决持久化接续核验.md"
evidence = ".planning/2026-10-27-recovery-outcome-review/committed-evidence.json"
records = {}
for name in ("negative-validation", "formal-negative-validation", "focused-validation", "final-validation"):
    record = json.loads((folder / (name + ".json")).read_text(encoding="utf-8"))
    owned = Path(record["owned_temp_directory"])
    assert record["temporary_data_removed"] and not owned.exists()
    assert owned.resolve().parent == (root / "outputs").resolve()
    records[name] = record
assert records["final-validation"]["snapshot_commit"] == source
assert records["final-validation"]["exit_code"] == 0
passed = int(re.findall(r"(\d+) passed", (folder / "final-tests.txt").read_text(encoding="utf-8"))[-1])
entry = {"date": "2026-10-27", "reviewer": "codex-runtime", "source_commit": source,
    "source_tree": records["final-validation"]["source_tree"], "report": report,
    "validation_records": records,
    "results": {"formal_negative": "9 failed, 7 passed", "focused": "54 passed", "fixed_related": f"{passed} passed"},
    "initial_probe_correction": "Initial 13 failed/1 passed included overriding _audit itself, bypassing its production Exception catch; not formal defect evidence. Formal probes inject audit.record and real state/gate event boundaries.",
    "owned_validation_scopes_removed": 4, "independent_acceptance_added": False,
    "status": "fixes_delivered_pending_independent_recheck",
    "not_run": ["full backend", "client/TypeScript", "device/APK", "real multi-process kill", "production"],
    "test_boundary": "Real App/SQLite/expired lease/recover_task and separate Database reader; sink failures injected; no real OS kill",
    "foreign_work_preserved": True,
    "log_sha256_lf": {p.name: hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest() for p in folder.glob("*.txt")}}
(folder / "committed-evidence.json").write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
(root / report).write_text(f"""# 恢复裁决持久化接续核验

2026-10-27，codex-runtime 接续 e5e3337/7de95f7 恢复对账修复。固定新修复 **{source}**。ALN-07-C 原owner=zcode-next、reviewer=zcode-main、一审=dsh-lead与review保留；本会话不自批旧/新补丁。

## 确认问题与修复

| 编号 | 级别 | 触发和结果 |
|---|---|---|
| RO-01 | P2 | recover_task支持SUSPENDED，但分歧分支直接WAITING_USER不在状态机允许边，抛IllegalTransition，未生成等待问题。现增加SUSPENDED→WAITING_USER，与已有RUNNING分歧路径一致；不扩展终态出边，不启动新run或重放工具 |
| RO-02 | P2 | _move先save_task并发state_transition，门禁后填waiting_request/failure_reason；状态事件故障可留下WAITING_USER无问题或FAILED无原因。现先构造裁决详情，再由_move一次保存状态与详情，删除冗余第二次save。事件异常仍传播，持久事实完整、_active释放，可从任务查询确认结果 |

## 验证

新增16项正式探针，修前 **9失败/7通过**；修后聚焦 **54通过**。固定提交归档13个相关模块 **{passed}通过**，覆盖本轮/上轮恢复、绑定/对账、运行所有权/续跑、审批、执行边界和状态机。测试数量与上一轮241的模块范围不同，不相加、不解释为总量下降。

探针使用真实App、SQLite、失效租约、累计预算检查点与recover_task，通过**另一条数据库连接**断言持久结果。覆盖RUNNING/SUSPENDED、计划/绑定两类错误、state_transition/reconciliation_gate两处事件故障、实际audit.record故障仍保持原尽力而为语义；断言无新run/工具调用/task_resumed且原检查点保留。

首版直接替换_audit令其抛异常，绕过生产方法已有捕获，得到13失败/1通过；原日志保留作开发探针历史，**不计入正式缺陷证据**。正式版在audit.record处注入，未将既有审计尽力而为行为误报成缺陷。

四处本轮独占验证临时目录已回收。原UI、matrix、review-6、29-E等其他会话在途内容、旧缓存与依赖保留。不是后端全量；本轮未重跑客户端/TS、真机/APK、真实多进程kill或生产供应商。

完整参数、归档/源码树摘要、原始日志及清理记录见[固定证据](../../../{evidence})。

## 接续

本轮补齐的是暂停分歧状态转换和裁决事实持久化。人工裁决的语义消费/再次对账、多候选人工绑定API、跨层统一视图与真实kill验收仍按原卡待接，不能把等待问题可查询等同于人工裁决闭环。新补丁需另一审阅人复核；父包与既有发布基线不因本轮回归关闭或发布。
""", encoding="utf-8")


def append(path, heading, body):
    target = root / path
    original = target.read_text(encoding="utf-8")
    if heading not in original:
        target.write_text(original.rstrip() + "\n\n" + heading + "\n\n" + body + "\n", encoding="utf-8")


append("docs/协作开发/交付/ALN-07-C-原子绑定精确步骤授权与对账消费.md", "## 2026-10-27 裁决持久化接续核验",
    f"33c22a1补齐SUSPENDED分歧→WAITING_USER合法边，状态与waiting_request/failure_reason一并保存，避免状态事件故障留下空裁决。16新正式探针修前9失败/7通过，聚焦54通过，固定13模块{passed}通过。详见[接续报告](../验收/2026-10-27-恢复裁决持久化接续核验.md)。原分工与review保留，新补丁待独立复核；人工裁决语义/再次对账、多候选API/真实kill仍待。")
append("docs/协作开发/验收/2026-10-27-恢复对账消费边界复核.md", "## 2026-10-27 裁决事实接续更正",
    f"上一轮241项保留为7de95f7历史证据。继续发现SUSPENDED分歧转换缺失、状态事件故障时裁决详情未随状态提交，33c22a1已修，固定相关13模块{passed}通过。见[接续报告](2026-10-27-恢复裁决持久化接续核验.md)。原正常错误测试未覆盖state_transition写失败，本次不以旧通过数代替新源证据。")
append("docs/协作开发/待接需求_2026-10-26.md", "## 二十二、恢复裁决持久化接续核验（2026-10-27）",
    f"ALN-07-C新增修复33c22a1：暂停分歧可合法转WAITING_USER，问题/失败原因与状态一次保存。正式16探针修前9失败/7通过，固定相关13模块{passed}通过；四处独占临时范围回收。见[报告](验收/2026-10-27-恢复裁决持久化接续核验.md)。\n\n需另一审阅人接续核对7de95f7/33c22a1，原owner/reviewer/一审和review保留。人工裁决语义消费与再次对账、多候选API、统一视图、真实kill仍待；不把问题可查询当裁决闭环，不关闭父包或发布。")

board_path = root / "docs/协作开发/workboard.json"
board = json.loads(board_path.read_text(encoding="utf-8"))
before = copy.deepcopy(board)
card = next(c for c in board["dispatches"] if c["id"] == "ALN-07-C")
assert (card["owner"], card["reviewer"], card["first_reviewer"], card["status"]) == ("zcode-next", "zcode-main", "dsh-lead", "review")
item = {"date": "2026-10-27", "reviewer": "codex-runtime", "source_commit": source, "report": report, "log": evidence,
    "scope": "暂停分歧等待转换、状态与裁决详情单次保存；独立SQLite连接核验事件失败持久事实",
    "result": f"16新正式探针；聚焦54；固定相关13模块{passed}通过；新补丁待独立复核"}
card["evidence"].append(item)
card["technical_review"] = {**item, "status": "changes_fixed_pending_independent_recheck", "evidence": evidence, "independent_acceptance_added": False}
card["latest_code_review"] = {"id": "REVIEW-2026-10-27-RECOVERY-OUTCOME", "date": "2026-10-27", "reviewer": "codex-runtime",
    "reviewed_commit": source, "scope": item["scope"], "report": report, "status": "changes_fixed_pending_independent_recheck"}
card["review_history"].append({"date": "2026-10-27", "previous_review_verdict": card["review_verdict"],
    "reason": "接续补齐暂停等待转换与事件故障持久事实，旧报告/证据保留，新补丁需独立复核", "report": report})
card["blocker"] = "7de95f7/33c22a1待独立复核；人工裁决语义消费/再次对账、多候选API、统一视图与真实kill仍待"
card["next_step"] = f"固定13模块{passed}通过；核对33c22a1暂停分歧和state_transition故障持久裁决，再接人工裁决/真实故障验收；" + report
files = subprocess.check_output(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", source], cwd=root, text=True, encoding="utf-8").splitlines()
board["post_candidate_changes"].append({"id": "review-fix-" + source[:12], "title": "fix(runtime): 一并持久化恢复裁决状态与详情", "workpackage": "ALN-07",
    "source_commit": source, "files": files, "status": "source_registered_pending_validation", "candidate_included": False, "independent_review": False,
    "report": report, "scope": f"恢复裁决持久事实；13模块{passed}通过；待独立复核"})
board["code_review_batches"].append({"id": "REVIEW-2026-10-27-RECOVERY-OUTCOME", "date": "2026-10-27", "reviewer": "codex-runtime",
    "cards": ["ALN-07-C"], "fixed_cards": ["ALN-07-C"], "fix_commit": source, "report": report, "evidence": evidence,
    "status": "fixes_delivered_pending_independent_recheck", "product_acceptance_added": False, "published_by_review": False,
    "scope": item["scope"]})
assert [c["id"] for c, old in zip(board["dispatches"], before["dispatches"]) if c != old] == ["ALN-07-C"]
board_path.write_text(json.dumps(board, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
print(f"ALN-07-C: fixed related {passed} passed, four scopes removed, no self-acceptance")
