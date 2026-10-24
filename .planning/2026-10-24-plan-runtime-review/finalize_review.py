"""Record the repaired runtime slice without accepting our own changes."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
prefix = folder.relative_to(root).as_posix()
evidence = json.loads((folder / "committed-evidence.json").read_text(encoding="utf-8"))
assert all(r["exit_code"] == 0 for r in evidence["results"])
commit = evidence["source_commit"]
short = commit[:7]
backend = (folder / "backend-committed.txt").read_text(encoding="utf-8")
passed, skipped = map(int, re.search(r"(\d+) passed, (\d+) skipped", backend).groups())
assert passed == 1602 and skipped == 1
assert not subprocess.check_output(["git", "diff", commit, "--", "evoagent", "tests"], cwd=root).strip()
report = "docs/协作开发/验收/2026-10-24-ALN-07-P运行时补审.md"
summary = f"07-P运行时补审七组修复（含域权限拒绝传输）；31项计划及7项传输探针原代码31失败/7通过，相关207通过；固定{short}后端{passed}/1，客户端同tree复用166/typecheck，未重跑真机，不发布基线"
related = folder / "related-final.txt"
related_evidence = json.loads((folder / "related-evidence.json").read_text(encoding="utf-8"))
assert related_evidence["source_commit"] == commit
assert related_evidence["results"][0]["sha256"] == hashlib.sha256(related.read_bytes()).hexdigest()
assert "207 passed" in related.read_text(encoding="utf-8")

board_path = root / "docs/协作开发/workboard.json"
board = json.loads(board_path.read_text(encoding="utf-8"))
card = next(c for c in board["dispatches"] if c["id"] == "ALN-07-P")
assert card["owner"] == "claude-main" and card["status"] == "review"
assert card.get("fix_commit") != commit, "Review metadata is already finalized"
card.setdefault("repair_history", []).append({k: card[k] for k in (
    "review_report", "review_note", "fix_status", "fix_author", "fix_commit") if k in card})
card.update(review_result="changes_requested", review_report=report,
    review_note="剩余ReAct/控制器恢复路径发现依赖绕过、坏计划降级、进度/失败归因及上下文问题，修复并补齐域鉴权拒绝传输；需其他审阅者复核，不自批",
    fix_author="codex-runtime", fix_commit=commit, fix_status="repaired_pending_independent_review",
    next_step=f"独立复核{short}及e54ad2a/eea3a00累计修复；原planner不变量切片的claude-main部分接受保持；精确动作/步骤绑定、对账与自动恢复策略消费仍另验")
card["peer_findings"] = list(dict.fromkeys(card.get("peer_findings", []) + [f"RT07-{i:02}" for i in range(1, 8)]))
card["shared_files"] = list(dict.fromkeys(card["shared_files"] + [
    "evoagent/planning/planner.py", "evoagent/core/controller.py", "evoagent/api/server.py", "evoagent/api/routes.py"]))
card["evidence"].append(dict(date="2026-10-24", reviewed_commit="50dd94c1eae841c26018762a6efedeb29eb1fe71",
    source_commit=commit, scope="剩余运行时切片补审；原有部分接受不扩大、不撤销；共享文件由codex-runtime串行整合",
    result=summary, report=report, log=prefix + "/committed-evidence.json"))
for package in board["workpackages"]:
    if package["id"] == "ALN-07":
        package["next_step"] = f"07-E的26aaec7与07-P运行时{short}修复待其他审阅者复核；07-W/T/R及P原planner不变量部分接受保持；工作流/轨迹/对账/自动恢复策略的实际消费另验，整包未关闭"
board["baseline"]["latest_source_verification"] = dict(source_commit=commit, source_tree=evidence["source_tree"],
    python_passed=passed, python_skipped=skipped, client_passed=166, typecheck="passed",
    scope=summary, evidence="../../" + prefix + "/committed-evidence.json")
board["latest_review_aln07p_runtime"] = dict(reviewer="codex-runtime", reviewed_commit="50dd94c1eae841c26018762a6efedeb29eb1fe71",
    fix_commit=commit, result="changes_requested; repaired_pending_independent_review", report=report)
board["updated_at"] = "2026-10-24"
board_path.write_text(json.dumps(board, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
spec = importlib.util.spec_from_file_location("checker", root / "tools/check_collaboration_plan.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)
checker.RENDERED.write_text(checker.render(board), encoding="utf-8", newline="\n")
assert not checker.validate(board), checker.validate(board)

report_text = f"""# ALN-07-P 运行时与恢复路径补审（2026-10-24）

结论：**changes_requested；已修复，待其他审阅者独立复核**。原作者claude-main；本轮审阅及修复codex-runtime。原planner修订不变量在7132180的第三方部分接受保留，不扩大到本轮运行时修复或父工作包。

起点`50dd94c`；累计源码提交`e54ad2a`、`eea3a00`、`{short}`。串行修改共享planner/controller/ReAct与API接线；没有复制计划、权限或副作用权威，没有修改作者原计划修订和续跑测试。

## 已发现并修复

| 编号 | 原问题与后果 | 当前行为 |
|---|---|---|
| RT07-01 | 当前步骤只按存储顺序取，失败/跳过依赖可照常执行；有独立就绪分支时仍可偷跑受阻工具 | 只选所有deps为done的步骤；无就绪步骤则在模型调用前plan_blocked；显式工具分支未就绪时拦截调用，失败祖先不可绕过。真实runner及账本均无新增记录 |
| RT07-02 | 损坏的持久计划被静默降级为无计划，真实App仍可能SUCCEEDED | present的空值/错误结构、重复/环/状态/完成证据冲突及错plan_ref明确报PlanRevisionError；模型/工具前校验。真正缺字段的旧任务兼容 |
| RT07-03 | submitted-only的ok被当成步骤完成 | 完成进度要求ok、executed与contract_valid同时成立；仅提交不产生completed_work |
| RT07-04 | 不同步骤失败串账、旧全局计数及无关工具能误伤当前步骤 | 失败键包含step_id/tool/error；真实成功清计数；无关tool_hint失败不把当前步骤标failed；旧键不混入新步 |
| RT07-05 | 恢复会保留过期计划提示，且停机原因未持久化 | 每次续跑刷新单一引擎计划上下文槽，保留执行trace；检查点记录stop_reason；控制器对plan_blocked使用SUSPENDED并给出原因 |
| RT07-06 | 已完成计划仍可产生新的额外工具动作 | 完成后允许总结/补充问题；额外工具被plan_complete拦截，不重放已完成步骤；Task成功仍由原Verifier判定 |
| RT07-07 | Goal等注册路由的域级admin拒绝未消费POST体，Windows客户端连接重置而非收到403 | 复用原有64KiB/250ms有界丢弃；RouteContext.body_read标记惰性JSON已消费，已读体不二读。保持原getter与原始字节fallback；权限规则不变 |

## 验证证据

- 将最终两份探针原样注入`50dd94c`的Git归档：**31 failed / 7 passed**，包含真实App、工具网关/SQLite账本及回环HTTP。不是通过改旧测试使其变绿。[探针哈希及原始复现](../../../{prefix}/before-evidence.json)。
- 相关回归**207 passed**，覆盖计划修订/轨迹、进程强杀恢复、审批/续跑、执行边界、07-E回归和Goal/权限接口；后续路由兼容补丁另通过88项。全量再次覆盖相同路径。[相关记录](../../../{prefix}/related-evidence.json)。
- 最终源码`{commit}`的独立Git归档：**{passed} passed / {skipped} skipped**，线程异常警告升级错误；声明/台账/认领/缺口映射/89主题覆盖五项检查通过。[全量记录](../../../{prefix}/committed-evidence.json)。
- Android目录Git tree与8c479b6一致，核对原日志哈希后复用166通过及类型检查；没有重跑真机、真实模型/供应商或空机依赖安装。
- 首轮`e54ad2a`为1589通过/1失败/1跳过，发现域拒绝的HTTP连接重置；第二轮`eea3a00`为1601通过/1失败/1跳过，发现旧基座要求原body getter身份。均保留失败证据，最终通过不覆盖历史。[首轮](../../../{prefix}/first-attempt-e54ad2a.json)、[第二轮](../../../{prefix}/second-attempt-eea3a00.json)。

## 仍需后续实现/验收的边界

- tool_hint是提示，不是稳定动作身份。同工具多步骤/参数尚无运行时step_bindings；无提示的通用准备步骤允许合法收集上下文，不能仅凭后续同名工具推断偷跑。修复覆盖可确定的受阻分支，不宣称完成精确步骤授权。
- 连续失败仍保留append恢复记录；依赖失败的恢复步骤不会自动执行。实际07-R结构化策略、07-E账本对账与宿主动作绑定/授权、工作流/轨迹消费仍另验，unknown不自动重放。
- 拒绝体丢弃有体积/时间上限；大体积或超时体仍直接关闭，不承诺任意慢/大请求都收到JSON。不解析或执行拒绝体。
- 04-UI四路径真机、13-A、07-E等独立审阅以及23包剩余设计差距不因本轮通过而关闭。40张子卡仍31 accepted/4 review/4 claimed/1 blocked；11-W未开工。
- 基线仍candidate，prepared_candidate保留22c50ca，published_commit为空；未纳入通知、backgroundRunner、DevicePackage及历史日志在途修改。
"""
(root / report).write_text(report_text, encoding="utf-8", newline="\n")

def edit(relative, transform):
    path = root / relative
    text = path.read_text(encoding="utf-8")
    updated = transform(text)
    assert updated != text, relative
    path.write_text(updated, encoding="utf-8", newline="\n")

note = f"最新补审：**运行时七组问题已修复，{short}待其他审阅者复核**。31项计划及7项传输探针在原源码31失败/7通过；相关207通过，固定源码{passed}/1。原planner不变量切片的第三方部分接受保持，见[运行时补审](../验收/2026-10-24-ALN-07-P运行时补审.md)。下文保留原交付时点，不把当时未完成的07-W/T/R描述当作当前状态。"
edit("docs/协作开发/交付/ALN-07-P-计划修订与证据保持.md", lambda t: t.split("\n", 1)[0] + "\n\n" + note + "\n" + t.split("\n", 1)[1])
edit("docs/协作开发/任务卡/07-计划修订与证据保持.md", lambda t: t + f"\n## 2026-10-24运行时补审\n\n{note}\n\n新增共享接线：planner.py、react/engine.py、core/controller.py及API server.py/routes.py；均在全局共享清单登记，由codex-runtime串行整合。运行态坏快照拒绝、依赖门控/失败归因/停止原因、惰性域权限拒绝体排空属于本轮范围。精确动作步骤绑定、对账与自动恢复消费仍另验，不新增账本权威。\n")
edit("docs/协作开发/README.md", lambda t: t.replace("本轮先审查最新07-E，不启动11-W。", "本轮补审07-P运行时及相关接口，不启动11-W。")
    .replace("07-E修复仍待独立接受。", "07-E及07-P运行时修复仍待独立接受。")
    .replace("07-E本轮修复、07-P剩余切片、13-A及04-UI继续单列。", "07-E及07-P运行时本轮修复、13-A及04-UI继续单列。")
    .replace("- 最近固定源码`26aaec7`归档验证：后端1566 passed/1 skipped，新增29项07-E独立审核探针；线程异常警告升级错误。声明、台账、认领、缺口映射及文档覆盖检查通过；客户端目录同tree，复用8c的166/typecheck，未重跑真机。见[证据](../../.planning/2026-10-24-aln07e-review/committed-evidence.json)。此前f633130的1521/1及05-I的1460/1保留历史，不扩大原接受范围。",
             f"- 最近固定源码`{short}`归档验证：后端{passed} passed/1 skipped；07-P运行时与域拒绝传输修复，相关207通过；线程异常警告升级错误，五项元数据/覆盖检查通过。客户端同tree复用8c的166/typecheck，未重跑真机。见[证据](../../{prefix}/committed-evidence.json)及[运行时补审](验收/2026-10-24-ALN-07-P运行时补审.md)。26aaec7的1566/1等保留历史，不扩大接受范围。")
    .replace("07-T/07-R/16-X已接受结论保留，不扩大到整包。", f"07-P剩余运行时路径也已补审修复，`{short}`待其他审阅者复核，见[补审记录](验收/2026-10-24-ALN-07-P运行时补审.md)。07-T/07-R/16-X已接受结论保留，不扩大到整包。"))
edit("docs/项目现状与审阅进展_2026-10-24.md", lambda t: t.replace("当前按`49667b9`任务状态及`26aaec7`的07-E审核修复更新", f"当前按既有独立接受、`26aaec7`的07-E及`{short}`的07-P运行时审核修复更新")
    .replace("固定最新`26aaec7`归档验证：后端**1566通过/1跳过**，相关回归125通过；五项元数据/覆盖检查通过。客户端同Git目录树复用8c479b6的166通过及类型检查，未重跑真机。[验证记录](../.planning/2026-10-24-aln07e-review/committed-evidence.json)。原f633130等验证保留历史。",
             f"固定最新`{short}`归档验证：后端**{passed}通过/1跳过**，本轮相关207通过；五项元数据/覆盖检查通过。客户端同Git目录树复用8c479b6的166通过及类型检查，未重跑真机。[验证记录](../{prefix}/committed-evidence.json)。07-P运行时七组问题已修复待独立复核，见[补审](协作开发/验收/2026-10-24-ALN-07-P运行时补审.md)；原26aaec7等验证保留历史。")
    .replace("07-W/T/R接受；07-P部分修复已复核；07-E对账已审核修复待复核 | E修复独立复核、P剩余恢复切片；真实工作流/轨迹/恢复/对账消费", "07-W/T/R接受；07-P原planner切片复核、本轮运行时修复待复核；07-E修复待复核 | P/E新修复独立复核；精确动作绑定、真实工作流/轨迹/自动恢复/对账消费")
    .replace("1. 独立复核07-E修复`26aaec7`，", f"1. 独立复核07-P运行时`{short}`及07-E修复`26aaec7`，"))
edit("docs/项目现状与设计差距核对_2026-10-23.md", lambda t: t.replace("新增07-E审核修复待复核，固定26aaec7为1566/1。", f"07-E及07-P运行时审核修复待复核，固定{short}为{passed}/1。"))
for name in ("findings.md", "progress.md"):
    with (folder / name).open("a", encoding="utf-8", newline="\n") as output:
        output.write(f"\n- 最终38项注入原源码31失败/7通过；相关207通过，路由兼容补丁88通过。固定{short}全量{passed}/1及五检查通过；同tree复用客户端166/typecheck。报告/台账更新但不自批、无新开发卡、未发布。\n")
edit(prefix + "/task_plan.md", lambda t: t.replace("|固定提交全量、审阅记录、台账提交|pending|", "|固定提交全量、审阅记录、台账提交|complete|"))
print(commit, passed, skipped)
