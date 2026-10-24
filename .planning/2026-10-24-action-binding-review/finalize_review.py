"""Record the reviewed binding slice and retain independent acceptance scopes."""
from collections import Counter
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
assert not subprocess.check_output(["git", "diff", commit, "--", "evoagent", "tests"], cwd=root).strip()
passed, skipped = map(int, re.search(r"(\d+) passed, (\d+) skipped", (folder / "backend-committed.txt").read_text(encoding="utf-8")).groups())
client = int(re.search(r"ℹ pass (\d+)", (folder / "client-committed.txt").read_text(encoding="utf-8")).group(1))
assert passed == 1653 and skipped == 1 and client == 207
report = "docs/协作开发/验收/2026-10-24-ALN-07-B独立审核.md"
summary = f"07-B六组独立审核修复；19项探针原源码15失败/4通过，相关241通过；固定{short}后端{passed}/1、客户端{client}/typecheck实际复跑，排除在途，未重跑真机，不发布基线"
related = folder / "related-final.txt"
related.write_text(related.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
assert "241 passed" in related.read_text(encoding="utf-8")
(folder / "related-evidence.json").write_text(json.dumps(dict(
    source_commit=commit, source_tree=evidence["source_tree"],
    scope="Named backend suites in working tree immediately before commit; evoagent/tests changes committed without further edits; full Git archive repeats all suites",
    results=[dict(name="binding-related", exit_code=0, result="241 passed", log=related.relative_to(root).as_posix(),
                  sha256=hashlib.sha256(related.read_bytes()).hexdigest())]), ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

board_path = root / "docs/协作开发/workboard.json"
board = json.loads(board_path.read_text(encoding="utf-8"))
card = next(c for c in board["dispatches"] if c["id"] == "ALN-07-B")
assert card["owner"] == "claude-main" and card["status"] == "review" and card.get("fix_commit") != commit
card.update(review_result="changes_requested", review_report=report,
    review_note="真实租约/多连接事务、工具身份、归属来源、缓存回执、LeaseLost及App装配六组问题已修复，修复作者codex-runtime须其他审阅者复核",
    fix_author="codex-runtime", fix_commit=commit, fix_status="repaired_pending_independent_review",
    peer_findings=[f"AB07-{i:02}" for i in range(1, 7)],
    next_step=f"独立复核{short}、19项探针及真实App/网关/租约路径；历史缓存无绑定时不补猜，执行前精确步骤授权、run/plan命名空间与自动消费对账结论另验")
card["shared_files"] = list(dict.fromkeys(card["shared_files"] + ["evoagent/app.py"]))
card["evidence"].append(dict(date="2026-10-24", reviewed_commit="ea5fc1563d05409199e90c84a4b1113613f2eae4",
    source_commit=commit, scope="07-B独立复核及六组修复；不自批，不扩大07-P/E接受范围", result=summary,
    report=report, log=prefix + "/committed-evidence.json"))
updates = {
    "ALN-07": f"07-P/W/T/R/E既定切片已第三方接受；07-B六组问题已修复{short}待其他审阅者复核；精确步骤授权、run/plan绑定与实际对账消费另验，整包未关闭",
    "ALN-11": "11-A/R既定切片已接受；11-W执行消费/探针/回滚自动化切片已复核，真机健康失败→回滚→旧能力执行仍待验，整包未关闭",
    "ALN-12": "12-A提议/执行/回执及离线故障自动化已复核，保持review/device_pending；App主链接线、真机断网/杀进程/重启与敏感证据另验，通知在途不计交付",
    "ALN-13": "13-A后端实体/十态/自主授权与Task动作接线已由claude-main独立接受；前端与生产自动推进另验，14/15已有消费不重复开发",
    "ALN-17": "17-A备份恢复/兼容拒绝/行数/迁移回退既定切片已由claude-main独立接受；组合资产恢复、空机部署及生产RTO/RPO另验",
    "ALN-20": "20-A五类Runtime协议/Mock与现有适配器/兼容矩阵已交付review，待独立复核；真实固定外部Harness PoC另验，不等同已集成",
}
for p in board["workpackages"]:
    if p["id"] in updates:
        p["next_step"] = updates[p["id"]]
    if p["id"] in ("ALN-12", "ALN-17"):
        p["delivery_state"] = "partial_verified"
    if p["id"] == "ALN-20":
        p["delivery_state"] = "needs_recheck"
board["baseline"]["latest_source_verification"] = dict(source_commit=commit, source_tree=evidence["source_tree"],
    python_passed=passed, python_skipped=skipped, client_passed=client, typecheck="passed", scope=summary,
    evidence="../../" + prefix + "/committed-evidence.json")
board["latest_review_aln07b"] = dict(reviewer="codex-runtime", reviewed_commit="ea5fc1563d05409199e90c84a4b1113613f2eae4",
    fix_commit=commit, result="changes_requested; repaired_pending_independent_review", report=report)
board_path.write_text(json.dumps(board, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
spec = importlib.util.spec_from_file_location("checker", root / "tools/check_collaboration_plan.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)
checker.RENDERED.write_text(checker.render(board), encoding="utf-8", newline="\n")
assert not checker.validate(board), checker.validate(board)
counts = Counter(c["status"] for c in board["dispatches"])
status = f"{len(board['dispatches'])}张子卡：{counts['accepted']} accepted、{counts['review']} review、{counts['blocked']} blocked"

def write(relative, text):
    (root / relative).write_text(text, encoding="utf-8", newline="\n")

write(report, f"""# ALN-07-B 动作绑定独立审核（2026-10-24）

结论：**changes_requested；六组问题已修复，待其他审阅者独立复核**。原作者claude-main，原交付827d46d，审核基点ea5fc15；本轮审核与修复codex-runtime，修复`{commit}`。原作者12项测试未修改；不自批、不撤销07-P/E既有接受。

## 发现与修复

| 编号 | 实际问题 | 修复行为 |
|---|---|---|
| AB07-01 | bind在租约下先BEGIN，幂等返回/冲突/参数异常未结束事务；下一次guarded write可能报嵌套事务。无租约的独立连接还存在先读后写竞争，抛SQLite唯一键异常而非BindingConflict | 租约和非租约路径均先取得写事务，再读/写/提交；每个异常回滚、幂等也结束事务；真实RunManager租约和两数据库连接验证 |
| AB07-02 | 相同effect/step/task但tool_id不同仍静默“幂等成功”，非法工具字段也被接受 | 非空工具标识校验，显式工具冲突抛BindingConflict，保留原行和首次bound_at；省略工具不改原值 |
| AB07-03 | 任意ToolResult.effect_id、仅run_id范围、无关/无提示步骤都能导出确定归属，07-E可能拒绝或产生虚假来源 | 要求明确task_id、当前显式工具匹配、真实网关账本任务/工具/运行及有副作用契约；不满足时记录step_binding_skipped，不按工具名找别的步骤 |
| AB07-04 | 历史确认回执命中时可能将旧effect补绑给新步骤；已有绑定则制造无意义冲突 | replay只保留已有相同任务/步骤绑定；缺失或其他步骤归属均跳过，不事后补猜、不重复runner、不覆盖旧归属 |
| AB07-05 | generic except把LeaseLost吞成best-effort证据错误，旧执行者继续推进 | LeaseLost原样穿透；普通存储故障仍记step_binding_error并保持真实外部结果；unknown仍unknown，不因绑定获得重放权限 |
| AB07-06 | 可选接线只在测试手动注入，生产App未装配，实际任务永不产生绑定 | App创建ActionBindings并注入既有ReAct，共用原数据库；真实App→网关→持久绑定→重开库→07-E对账全链验证 |

## 证据

- 最终19项独立探针注入ea5fc15 Git归档，**15 failed / 4 passed**。通过的正向控制包括真实双步骤、同步骤回执、unknown保真及普通存储故障；测试失败数不等于独立根因数。[原源码与探针哈希](../../../{prefix}/before-evidence.json)。
- 相关回归**241 passed**，含原作者12项、ReAct/07-E/计划恢复、进程恢复、身份与预算主链。[相关证据](../../../{prefix}/related-evidence.json)。
- 固定`{short}` Git归档全量 **{passed} passed / {skipped} skipped**，线程异常警告视为错误；客户端在同归档实际重跑 **{client} passed**、类型检查通过；声明/台账/认领/缺口映射/89主题覆盖检查通过。[全量证据](../../../{prefix}/committed-evidence.json)。
- 未收集通知/后台runner/DevicePackage和历史日志在途；没有重跑真机、真实供应商或外部Runtime。TypeScript验证复用本机安装的依赖，不声称空机安装已验。

## 契约与后续边界

- 绑定仍是证据，不能授权；没有更改账本状态、unknown裁决、权限或计划完成规则。旧库无表仍由幂等域DDL建表，未引入列迁移。
- 不确定来源用step_binding_skipped表达；普通存储错误用step_binding_error表达；这两个字段随ReAct结果/检查点保留。lease丢失必须停止。
- ActionBindings直接bind仍是可信宿主记录接口，不能证明任意调用者传入的effect/step真实性；真实ReAct消费端现在验证网关账本来源。手动脚本不获得新权限。
- 无工具提示的通用准备步骤、同工具不同参数、跨计划重用步骤ID仍不能证明语义一致。当前表按task而非run/plan导出，宿主需筛选对应记录，精确命名空间另验。
- 外部副作用成功到绑定写入之间仍有崩溃窗口；旧确认回执没有绑定时保留缺证据，不能恢复出不存在的归属。将绑定前置到执行网关/原子操作仍属后续实现。
- 07-E如何驱动告警、阻断或恢复未自动接入；精确步骤执行前授权仍另验。不能用本轮归因修复宣布07整包闭合。
- 当前{status}。07-P、13-A、17-A新独立接受均保留；11-W/12-A为automation_accepted_device_pending；20-A仍待审。00-B仍blocked，candidate/22c50ca/published_commit=null不变。
""")
note = f"最新独立审核：**六组问题已修复，{short}待其他审阅者复核**。19探针原源码15失败/4通过，相关241通过；固定源码{passed}/1、客户端{client}/typecheck实际复跑。事务、可信来源、历史缓存、租约和App装配见[独立审核](../验收/2026-10-24-ALN-07-B独立审核.md)。下文是原交付/任务要求，当前行为以修复记录为准。"
for rel in ("docs/协作开发/交付/ALN-07-B-运行时动作绑定.md", "docs/协作开发/任务卡/07-B运行时动作绑定.md"):
    text = (root / rel).read_text(encoding="utf-8")
    title, rest = text.split("\n", 1)
    write(rel, title + "\n\n" + note + "\n" + rest)

readme = root / "docs/协作开发/README.md"
text = readme.read_text(encoding="utf-8")
text = re.sub(r"40张子卡为[^；]+", status, text, count=1)
start, rest = text.split("## 当前安排\n", 1)
_, end = rest.split("## 代码与发布现状", 1)
text = start + f"""## 当前安排

当前项目的zcode-main初始四卡均已交付，队列收尾至00-B集成协调（blocked）；没有in_progress卡，WIP上限仍为1。codex-runtime本轮独立复核07-B并修复六组问题，待其他审阅者复核；共享文件串行，不纳入通知/后台在途。

| 子卡 | 当前范围与状态 |
|---|---|
| 07-P / 13-A / 17-A | 既定切片已由claude-main独立接受 |
| 07-B | 动作绑定已审核修复`{short}`，待另一审阅者接受 |
| 11-W / 12-A | 自动化切片已复核，保留review，真机验收待补 |
| 20-A | Runtime协议/适配与矩阵已交付review，固定外部PoC另验 |
| 04-UI / 00-B | 审批四路径真机待验；基线仍未发布 |

实际开工与独立审阅见[认领表](开发任务认领表.md)，子卡接受不关闭父工作包。17-A第三方接受及其他作者成果保持归属，后续进度以实时workboard为准。

## 代码与发布现状""" + end
text = re.sub(r"- 最近固定源码[^\n]+", f"- 最近固定源码`{short}`归档验证：后端{passed} passed/1 skipped，相关241通过；客户端{client}通过及typecheck为本轮实际复跑，五项元数据/覆盖检查通过，未重跑真机。见[证据](../../{prefix}/committed-evidence.json)与[07-B独立审核](验收/2026-10-24-ALN-07-B独立审核.md)。此前aafca22的1602/1保留历史。", text, count=1)
text = text.replace("07-P运行时本轮修复、13-A及04-UI继续单列。", "07-P运行时、13-A与17-A已独立接受；07-B本轮修复、20-A与04-UI继续待审，11-W/12-A真机待验。")
write("docs/协作开发/README.md", text)

path = root / "docs/项目现状与审阅进展_2026-10-24.md"
text = path.read_text(encoding="utf-8")
text = re.sub(r"当前按既有独立接受[^\n]+", f"当前按`{short}`的07-B审核修复及07-P、13-A、17-A等最新独立接受更新。保留各切片结论；原89主题/20系统用例映射见[前次完整对照](项目现状与设计差距核对_2026-10-23.md)。", text, count=1)
text = re.sub(r"当前有\*\*[^\n]+", f"当前有**{status}**，无claimed/in_progress。接受是各自切片的结论，不表示23个设计工作包全量验收完成。发布仍candidate，published_commit为空，prepared_candidate仍为历史22c50ca。", text, count=1)
text = re.sub(r"固定最新[^\n]+", f"固定最新`{short}`归档验证：后端**{passed}通过/1跳过**，本轮相关241通过；五项元数据/覆盖检查通过。客户端在同归档实际复跑{client}通过及类型检查，未重跑真机。[验证记录](../{prefix}/committed-evidence.json)。07-B六组问题已修复待独立复核，[审核记录](协作开发/验收/2026-10-24-ALN-07-B独立审核.md)。旧结果保留历史。", text, count=1)
table_rows = {
    "ALN-07 计划": "07-P/W/T/R/E接受；07-B六组问题审核修复待复核 | 绑定run/plan命名空间、精确步骤授权、实际工作流/轨迹/自动恢复/对账消费",
    "ALN-11 更新": "11-A/R接受；11-W自动化切片已复核、真机待验 | 真机健康失败→回滚→旧能力执行证据，主链及硬超时另验",
    "ALN-12 手机离线": "12-A协议/离线自动化已复核，保持review/device_pending | App主链接线、真机断网/杀进程/重启及敏感证据；通知在途不计交付",
    "ALN-13 Goal": "13-A实体/十态/自主授权既定切片独立接受 | 前端入口、生产自动推进；14/15已有消费",
    "ALN-17 灾备": "17-A备份/恢复/拒绝/行数与回退切片独立接受 | 组合资产恢复、空机部署和生产RTO/RPO",
    "ALN-20 Runtime": "20-A协议/Mock与现有适配/矩阵已交付review | 独立复核、真实固定外部Harness PoC与主链采用判据",
}
for label, row in table_rows.items():
    text = re.sub(r"\| " + re.escape(label) + r" \|[^\n]+", "| " + label + " | " + row + " |", text)
head, _ = text.split("## 下一步\n", 1)
text = head + f"""## 下一步

1. 独立复核07-B修复`{short}`：事务/租约、实际来源、历史缓存与App主链；原作者12项测试保持不变。
2. 独立审核20-A；11-W/12-A保留自动化复核结果并补真机，04-UI补批准/拒绝/离线/过期四路径。
3. 初始11-W→12-A→17-A→20-A均已交付，不再写成排队未开工；当前00-B集成协调仍blocked，不禁止后续独立开发。
4. 00-B需选定发布范围、独立接受和干净候选证据；全量绿不替代生产/真机验收，也不自动发布。

实时结构化状态见[workboard.json](协作开发/workboard.json)，生成版本见[认领表](协作开发/开发任务认领表.md)。
"""
write("docs/项目现状与审阅进展_2026-10-24.md", text)
path = root / "docs/项目现状与设计差距核对_2026-10-23.md"
text = path.read_text(encoding="utf-8")
lines = text.splitlines()
lines[2] = f"最新状态见[2026-10-24审阅进展](项目现状与审阅进展_2026-10-24.md)：{status}；07-P/13-A/17-A已独立接受，07-B修复待复核；固定{short}为{passed}/1、客户端{client}/typecheck实际复跑。**下文为becfad7历史盘点和后续补记，不用于读取当前任务状态。**"
write("docs/项目现状与设计差距核对_2026-10-23.md", "\n".join(lines) + "\n")
for name in ("findings.md", "progress.md"):
    with (folder / name).open("a", encoding="utf-8", newline="\n") as output:
        output.write(f"\n- 最终固定源码{short}全量{passed}/1、客户端{client}/typecheck及五检查通过；19探针原代码15失败/4通过、相关241通过。记录六组修复但07-B仍review，保留07-P/13-A/17-A独立接受和11-W/12-A自动化/真机边界。\n")
path = folder / "task_plan.md"
path.write_text(path.read_text(encoding="utf-8").replace("| 固定源码验证、记录与提交 | pending |", "| 固定源码验证、记录与提交 | complete |"), encoding="utf-8", newline="\n")
print(commit, passed, client, status)
