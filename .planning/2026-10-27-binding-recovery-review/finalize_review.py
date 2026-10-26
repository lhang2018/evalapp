"""Register fixed-source evidence without accepting our own recovery patch."""
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
source = "7de95f79deef83c0b10fcff1cad3dab56d85225a"
report = "docs/协作开发/验收/2026-10-27-恢复对账消费边界复核.md"
evidence = ".planning/2026-10-27-binding-recovery-review/committed-evidence.json"


def git(*args):
    return subprocess.check_output(["git", *args], cwd=root, text=True, encoding="utf-8").strip()


records = {}
for name in ("negative-validation", "error-negative-validation", "focused-validation",
             "final-focused-validation", "committed-validation", "final-validation"):
    value = json.loads((folder / (name + ".json")).read_text(encoding="utf-8"))
    owned = Path(value["owned_temp_directory"])
    assert owned.resolve().parent == (root / "outputs").resolve()
    assert value["temporary_data_removed"] and not owned.exists()
    records[name] = value
final = records["final-validation"]
assert final["snapshot_commit"] == source and final["exit_code"] == 0
log = (folder / "final-tests.txt").read_text(encoding="utf-8")
passed = int(re.findall(r"(\d+) passed", log)[-1])
assert passed > 169
result = f"{passed} passed"
entry = {
    "date": "2026-10-27", "reviewer": "codex-runtime", "source_commit": source,
    "source_tree": final["source_tree"], "report": report,
    "fix_commits": [source], "validation_records": records,
    "results": {"initial_negative": "7 failed, 2 passed", "extra_error_negative": "4 failed, 9 deselected",
                "initial_focused": "1 failed, 77 passed", "final_focused": "86 passed",
                "fixed_related": result},
    "initial_fixed_attempt": "No tests ran: mistyped nonexistent test_host_deployment_review.py; corrected file selection in final-validation",
    "owned_validation_scopes_removed": len(records), "independent_acceptance_added": False,
    "status": "fixes_delivered_pending_independent_recheck",
    "not_run": ["full backend", "client/TypeScript", "device/APK", "real multi-process kill", "production providers"],
    "continuation_test_boundary": "Real App/Database/expired lease/recover_task; new clean probes stub only _run to assert gate admits continuation; existing clean e2e also retained",
    "foreign_changes_and_old_caches_preserved": True,
    "log_sha256_lf": {p.name: hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
                      for p in folder.glob("*.txt")},
}
(folder / "committed-evidence.json").write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

text = f"""# 恢复对账消费边界复核

2026-10-27，codex-runtime 接续 f8f32f7 的 ALN-07-C 一审，起点 201936f。固定修复 **{source}**。原 owner=zcode-next、分配 reviewer=zcode-main、一审人=dsh-lead 保留，卡片仍 review；本会话旧/新补丁均不自批。

## 确认问题与修复

| 编号 | 级别 | 触发、影响与处理 |
|---|---|---|
| RG-01 | P2 | 已完成 current_time/file_read 的真实计划含 tool_hint，恢复门禁直接当副作用声明，账本无回执即误判 unexecuted→WAITING_USER。旧干净 e2e 删除 hint 绕开问题。现消费端根据工具合约复制副作用投影，仅对明确 side_effect_level=none 清除 hint；持久计划、步骤、完成证据和账本行不改。未知工具保留声明，矛盾只读回执仍 undeclared，不借过滤放行 |
| RG-02 | P2 | 门禁在恢复活动登记的清理保护之外，history 读取/事件写入异常留下 _active，下一次恢复报正在运行。现门禁到 runs.begin 均受 BaseException 清理保护；历史读取也进入门禁错误记录范围。事件失败仍传播原异常，但释放登记，允许重试 |
| RG-03 | P2 | 计划不可读/绑定故障时原门禁尝试 RUNNING/SUSPENDED→FAILED，违反状态机，原始错误被 IllegalTransition 遮盖。现经 RECOVERING→FAILED；新增合法 SUSPENDED→RECOVERING 边，中间状态不落库，不创建新 run 或改写累计预算检查点。失败原因持久化并记录门禁事件 |

## 验证与证据边界

- 新增13项探针：只读两工具/混合计划真实恢复，未知工具与真实副作用缺证，矛盾只读回执，账本读故障，RUNNING/SUSPENDED 下计划/绑定故障四组合，OSError/KeyboardInterrupt 事件失败后同任务可重试。
- 初始9项 **7失败/2通过**；追加状态错误4项 **4失败/9未选**。首轮聚焦 **77通过/1失败** 继续暴露状态机问题；最终聚焦 **86通过**。
- 固定提交归档、16个相关模块 **{result}**，覆盖绑定/对账、租约/恢复/审批、执行边界/状态机、资源准入、部署及三库恢复。不是后端全量；本轮未重跑客户端/TS、设备/APK、真实多进程 kill 或生产供应商。
- 新正常恢复探针使用真实 App/SQLite、失效租约、检查点和 recover_task，仅替换 _run 记录放行续跑；既有干净 e2e 已取消删除 tool_hint 的绕行。没有把受控续跑当实际模型业务验收。
- 首次固定回归误写不存在的 test_host_deployment_review.py，**未执行测试**；原 committed-tests.txt/committed-validation.json 保留，纠正选择后以 final-tests.txt/final-validation.json 为最终证据。
- 六处本轮独占验证临时范围全部回收，历史缓存/依赖和他会话 UI、matrix、review-6 进度等保留。

原始日志、完整参数、源码树、归档摘要和清理记录：[固定证据](../../../{evidence})。

## 接续与审阅口径

f8f32f7 的169通过与6项绑定探针保留为一审历史；不能据此声称真实只读计划恢复或异常终态已覆盖。台账还保留2026-10-25 zcode-main早期接受及后续重开历史，2026-10-26“未复核”只应理解为当轮补丁尚待复核，不能抹去早期审阅。

新补丁需另一审阅人确认。WAITING_USER(reconciliation) 人工裁决消费、多候选裁决 API、跨层统一事件视图、真实多进程故障注入沿用原卡缺口；无计划/无绑定 legacy 标注放行语义未扩大验收。整包和发布基线均不因本次相关回归关闭或发布。
"""
(root / report).write_text(text, encoding="utf-8")


def append_once(path, marker, body):
    target = root / path
    current = target.read_text(encoding="utf-8")
    if marker not in current:
        target.write_text(current.rstrip() + "\n\n" + marker + "\n\n" + body + "\n", encoding="utf-8")


append_once("docs/协作开发/交付/ALN-07-C-原子绑定精确步骤授权与对账消费.md",
            "## 2026-10-27 恢复消费接续修复",
            f"7de95f7修复只读tool_hint误当副作用声明、门禁异常活动登记泄漏、非法FAILED转换。新13项探针，固定相关{result}，最终聚焦86通过；旧13/156/169数字均为历史，不替代新源证据。详见[接续报告](../验收/2026-10-27-恢复对账消费边界复核.md)。原owner与分配reviewer zcode-main、一审dsh-lead保留；新补丁需独立确认，人工裁决/多候选API/真实kill/统一视图仍待。")
delivery = root / "docs/协作开发/交付/ALN-07-C-原子绑定精确步骤授权与对账消费.md"
delivery.write_text(delivery.read_text(encoding="utf-8").replace("审阅人：待定（须独立复核，不接受自审自收）", "审阅人：zcode-main（按当前台账；一审 dsh-lead，新补丁须独立复核）"), encoding="utf-8")
append_once("docs/协作开发/验收/2026-10-26-dsh复核-07C.md", "## 2026-10-27 接续发现与修复",
            f"旧169通过/6项绑定探针保留为历史。接续发现只读工具声明误判、门禁异常活动登记泄漏及非法失败转换，7de95f7已修；固定相关{result}。原干净e2e曾清除tool_hint，新测试保留真实字段。详见[接续报告](2026-10-27-恢复对账消费边界复核.md)。卡片仍review，新补丁需另一审阅人确认；台账保留zcode-main早期审阅和后续重开历史，不能概括为从未审阅。")
append_once("docs/协作开发/待接需求_2026-10-26.md", "## 二十一、恢复对账消费边界复核（2026-10-27）",
            f"ALN-07-C接续f8f32f7一审，7de95f7修复只读计划误拦、恢复异常活动登记泄漏与非法FAILED转换。新13项探针，固定相关{result}，非全量后端/客户端/设备/真实多进程kill。见[报告](验收/2026-10-27-恢复对账消费边界复核.md)。\n\n| 待接项 | 当前状态与下一步 |\n|---|---|\n| 07-C新补丁 | owner=zcode-next、reviewer=zcode-main、一审dsh-lead保留，仍review；需独立复核7de95f7，不自批旧补丁 |\n| 恢复裁决 | WAITING_USER人工裁决消费、多候选裁决API、leases/gate统一事件视图和真实kill注入仍待，不关闭父包或发布基线 |\n\n六处新验证临时范围已回收，原缓存/依赖和其他会话内容保留；旧一审数字只作历史证据。")

board_path = root / "docs/协作开发/workboard.json"
board = json.loads(board_path.read_text(encoding="utf-8"))
before = copy.deepcopy(board)
card = next(c for c in board["dispatches"] if c["id"] == "ALN-07-C")
assert (card["owner"], card["reviewer"], card["first_reviewer"], card["status"]) == ("zcode-next", "zcode-main", "dsh-lead", "review")
item = {"date": "2026-10-27", "reviewer": "codex-runtime", "source_commit": source,
        "report": report, "log": evidence, "result": f"13新探针；聚焦86；固定相关{result}；三类恢复消费缺陷修复，待独立复核",
        "scope": "只读声明/保守证据处理、门禁异常收尾、合法失败转换；非真机或真实kill"}
if item not in card["evidence"]:
    card["evidence"].append(item)
verdict = "changes_fixed_pending_independent_recheck"
if card.get("review_verdict") != verdict:
    card.setdefault("review_history", []).append({"date": "2026-10-27", "previous_review_verdict": card.get("review_verdict"),
        "reason": "新负向探针确认三类真实恢复消费缺陷，补丁已交付待独立复核；一审历史保留", "report": report})
card["review_verdict"] = verdict
card["technical_review"] = {**item, "status": verdict, "evidence": evidence, "independent_acceptance_added": False}
card["latest_code_review"] = {"id": "REVIEW-2026-10-27-RECOVERY-GATE", "date": "2026-10-27",
    "reviewer": "codex-runtime", "reviewed_commit": source, "report": report, "status": verdict,
    "scope": "三类恢复消费边界修复；新/旧自有补丁不自批"}
card["blocker"] = "7de95f7新补丁待独立复核；人工裁决消费/多候选裁决API、统一事件视图与真实多进程kill仍待"
card["next_step"] = f"固定相关{result}；另审阅人核对7de95f7只读计划/异常活动登记/合法失败转换，之后继续人工裁决与真实故障验收。保留原owner/reviewer/一审人和早期接受后重开历史；" + report
change = {"id": "review-fix-" + source[:12], "title": "fix(runtime): 修正恢复对账声明与异常收尾", "workpackage": "ALN-07",
    "source_commit": source, "files": git("diff-tree", "--no-commit-id", "--name-only", "-r", source).splitlines(),
    "status": "source_registered_pending_validation", "candidate_included": False, "independent_review": False,
    "report": report, "scope": f"恢复对账三类边界；固定相关{result}；新补丁待独立接受"}
if not any(c["id"] == change["id"] for c in board["post_candidate_changes"]):
    board["post_candidate_changes"].append(change)
batch = {"id": "REVIEW-2026-10-27-RECOVERY-GATE", "date": "2026-10-27", "reviewer": "codex-runtime",
    "cards": ["ALN-07-C"], "fixed_cards": ["ALN-07-C"], "fix_commit": source,
    "report": report, "evidence": evidence, "status": "fixes_delivered_pending_independent_recheck",
    "product_acceptance_added": False, "published_by_review": False,
    "scope": "接续一审：只读计划与异常收尾，保留历史审阅与原分工"}
if not any(c["id"] == batch["id"] for c in board["code_review_batches"]):
    board["code_review_batches"].append(batch)
changed_cards = [c["id"] for c, old in zip(board["dispatches"], before["dispatches"]) if c != old]
assert changed_cards == ["ALN-07-C"]
board_path.write_text(json.dumps(board, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
print(f"registered ALN-07-C; {result}; six validation scopes removed; no independent acceptance")
