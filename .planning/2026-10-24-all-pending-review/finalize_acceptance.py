"""Apply the user's explicit unified acceptance mandate with honest scopes."""
import importlib.util
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2]
folder = Path(__file__).resolve().parent
prefix = folder.relative_to(root).as_posix()
evidence = json.loads((folder / "committed-evidence.json").read_text(encoding="utf-8"))
assert all(r["exit_code"] == 0 for r in evidence["results"])
commit = evidence["source_commit"]
assert commit.startswith("07466fb")
assert "1661 passed, 1 skipped" in (folder / "backend-committed.txt").read_text(encoding="utf-8")
assert "ℹ pass 228" in (folder / "client-committed.txt").read_text(encoding="utf-8")
assert not subprocess.check_output(["git", "diff", commit, "--", "evoagent", "tests", "android-app/App.tsx",
    "android-app/src/update", "android-app/src/device/broker.ts", "android-app/src/device/taskProtocol.ts"], cwd=root).strip()
report = "docs/协作开发/验收/2026-10-24-全部待审核统一验收.md"
request = "所有待审核的不管安排谁，你直接复合验收即可"
ids = ["ALN-04-UI", "ALN-07-B", "ALN-11-W", "ALN-12-A", "ALN-20-A"]
scopes = {
    "ALN-04-UI": "审批详情/单次决定/权限与离线状态；批准/拒绝/断网恢复/过期真机证据已交叉复核。演示服务无鉴权，角色限制另由真实API自动化覆盖",
    "ALN-07-B": "cc0aa3a六组修复复验：租约/并发事务、真实归属、缓存回执与App装配；精确动作授权、plan/run命名空间和自动对账仍不在本切片",
    "ALN-11-W": "执行消费、有界探针、制品绑定与回滚恢复；既有真机失败→回滚→旧能力执行/重启证据已核对。新增硬超时与竞态修复由固定源码回归覆盖",
    "ALN-12-A": "本卡正式范围为提议/回执/任务引用与故障矩阵；并发认领、持久去重、隐私、严格风险及坏状态拒绝已修复验证。自然语言App主链/真机设备全矩阵属于父包后续",
    "ALN-20-A": "五类Runtime协议、现有/Mock共享夹具与能力矩阵切片；能力类型/缺接口/重复声明/Mock真实拒绝与事件顺序已修复。固定外部Runtime实测与正式采用另验",
}
next_steps = {
    "ALN-04-UI": "本卡既定UI与四路径证据已按用户授权验收；生产身份与完整端到端故障矩阵仍按父包推进",
    "ALN-07-B": "本轮用户授权复验通过；网关原子绑定/崩溃缺证、plan/run精确范围与对账驱动执行另行实现",
    "ALN-11-W": "本卡执行消费/探针/实际回滚与旧包执行切片已验收；生产签名、多设备灰度与更多真机超时故障另验",
    "ALN-12-A": "协议与矩阵切片已验收；另拆自然语言Task→设备动作App主链、物理重启/系统杀进程全矩阵与敏感数据真机验收，不关闭父包",
    "ALN-20-A": "协议与矩阵切片已验收；固定外部Runtime commit的真实PoC、ADR正式采用与实际主链接入另行推进",
}
board_path = root / "docs/协作开发/workboard.json"
board = json.loads(board_path.read_text(encoding="utf-8"))
assert {c["id"] for c in board["dispatches"] if c["status"] == "review"} == set(ids)
for card in board["dispatches"]:
    if card["id"] not in ids:
        continue
    card.setdefault("review_history", []).append({k:card[k] for k in (
        "reviewer", "review_result", "review_note", "review_report", "fix_status") if k in card})
    card["reviewer"] = "codex-runtime"
    card["status"] = "accepted"
    card["review_result"] = "accepted"
    card["review_report"] = report
    card["review_note"] = "用户明确要求本会话统一复验验收；本轮自己的修复属于授权复验而非第三方。" + scopes[card["id"]]
    card["review_authorization"] = dict(source="user_request", request=request, allow_same_author=True, independent=False)
    card["acceptance_review"] = dict(date="2026-10-24", reviewer="codex-runtime", source_commit=commit,
        mode="user_authorized_revalidation", independent=False, scope=scopes[card["id"]], report=report)
    card["next_step"] = next_steps[card["id"]]
    if card["id"] == "ALN-07-B":
        card["fix_status"] = "fix_verified_by_user_authorized_revalidation"
    if card["id"] in ("ALN-11-W", "ALN-12-A", "ALN-20-A"):
        card.update(fix_author="codex-runtime", fix_commit=commit, fix_status="fix_verified_by_user_authorized_revalidation")
    card["evidence"].append(dict(date="2026-10-24", source_commit=commit, scope=scopes[card["id"]],
        result="fixed Git archive: backend1661 passed/1 skipped; client228 passed; typecheck passed; 5 metadata/coverage checks passed",
        report=report, log=prefix + "/committed-evidence.json"))

shared = ["android-app/src/update/manager.ts", "android-app/src/device/broker.ts", "android-app/src/device/taskProtocol.ts"]
for path in shared:
    if path not in {s["path"] for s in board["shared_files"]}:
        board["shared_files"].append(dict(path=path, stream="android"))
for card in board["dispatches"]:
    if card["id"] == "ALN-11-W":
        card["shared_files"] = list(dict.fromkeys(card["shared_files"] + shared[:1]))
    if card["id"] == "ALN-12-A":
        card["shared_files"] = list(dict.fromkeys(card["shared_files"] + shared[1:]))
    if card["id"] == "ALN-00-B":
        card["blocker"] = "最新准备候选07f11e5不含07466fb执行/去重/能力修复；需刷新选定候选与干净核验后另行发布，published_commit仍空"
        card["next_step"] = "五张待审卡已统一验收；把候选刷新到包含07466fb与本轮元数据的提交并核验，再执行发布流程，不发布旧07f11e5"
updates = {
    "ALN-04": "04-R及04-UI既定切片已接受，UI四路径真机证据已核对；更多生产组合恢复与身份部署仍另验",
    "ALN-07": "07-P/W/T/R/E/B既定切片均已验收；原子绑定崩溃窗口、精确步骤授权/范围与实际对账消费仍待实现，整包未关闭",
    "ALN-11": "11-A/R/W既定切片均已验收，健康失败→真实回滚→旧包执行/重启真机证据已核对；生产签名/多端灰度另验",
    "ALN-12": "12-A协议/矩阵及本轮安全边界修复已验收；自然语言App主链与父包真机离线/系统重启矩阵另拆，不将协议通过当整包闭合",
    "ALN-20": "20-A协议/适配/矩阵切片已验收；固定外部Runtime真实PoC与主链采用另验，整包未关闭",
}
for package in board["workpackages"]:
    if package["id"] in updates:
        package["next_step"] = updates[package["id"]]
board["baseline"]["latest_source_verification"] = dict(source_commit=commit, source_tree=evidence["source_tree"],
    python_passed=1661, python_skipped=1, client_passed=228, typecheck="passed",
    scope="全部五张待审卡统一验收；Runtime/设备协议/工作流边界修复，固定Git归档1661/1+228+typecheck；真机为657ed1c/07f11e5既有采集的只读复核；不发布基线",
    evidence="../../" + prefix + "/committed-evidence.json")
board["baseline"]["latest_acceptance"] = dict(source_commit=commit, accepted_cards=ids, reviewer="codex-runtime",
    authorization="user-requested unified revalidation; not claimed independent", report=report,
    publication_ready=False, publication_blocker="prepared_candidate07f11e5 omits 07466fb runtime/client fixes")
board_path.write_text(json.dumps(board, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
spec = importlib.util.spec_from_file_location("checker", root / "tools/check_collaboration_plan.py")
checker = importlib.util.module_from_spec(spec); spec.loader.exec_module(checker)
checker.RENDERED.write_text(checker.render(board), encoding="utf-8", newline="\n")
assert not checker.validate(board), checker.validate(board)

report_text = f"""# 全部待审核卡统一复验与验收（2026-10-24）

按用户原话“{request}”，由codex-runtime统一复核五张待审卡。**五卡既定切片均通过验收，转accepted；当前40 accepted、0 review、1 blocked。** 作者和历史审阅归属保留。本轮自己的修复属于用户授权复验，**不是第三方独立接受**；不把卡片验收扩大为父包全量完成。

## 逐卡结论

| 卡 | 已核实与接受的范围 | 保留的后续范围 |
|---|---|---|
| 04-UI | 真实审批组件状态/错误/凭据隔离与API权限回归；657ed1c/07f11e5采集中的批准、拒绝、断网恢复和过期证据已交叉核对 | 本地演示无鉴权、注入设备任务映射，不冒充生产IAM或全任务提交主链 |
| 07-B | cc0aa3a六组修复及19探针、App/租约/真实账本路径复验 | 副作用完成到绑定之间崩溃窗口、run/plan范围、精确授权和自动对账消费 |
| 11-W | 执行消费、工具有界等待、探针/制品身份/回滚恢复；已有真机真实回滚、旧包执行与重启证据核对 | 生产签名根、多设备灰度及更多硬件故障矩阵 |
| 12-A | 正式任务卡首份范围是协议/引用/回执/故障矩阵；并发、去重、隐私、风险、坏持久记录修复通过 | 自然语言Task→设备动作App主链、系统杀进程/物理重启全矩阵为父包后续，不在本卡冒充完成 |
| 20-A | 五能力协议、Mock/真实适配共享夹具、拒绝语义和矩阵 | 固定外部Runtime commit的真实PoC、ADR正式采用与生产接入 |

## 新发现及已修复

- **20-A**：非布尔supported、重复声明可被接受/字典覆盖；仅注入object即冒充有能力；Mock旧flag能虚假声明沙箱；Mock事件查询与真实EventBus的前缀/limit=0不一致。现严格校验类型、实际方法与重复能力，Mock始终拒绝真实沙箱，查询顺序一致。真实沙箱connect限制和工具权限拒绝通过；没有修改既有沙箱权限。
- **12-A**：两个实例可能并发下发同一动作；同action_id可替换任务/run/effect/参数；传入对象可变；容量淘汰或坏台账清空后可能重放；日志缺失被记为确定failed；高风险能被显式low降级；期望屏幕文本/收件目标进入上传。现同KV/key序列化预认领、快照与身份校验，淘汰后留持久去重标记，未决记录不淘汰，损坏记录拒绝，缺日志保持unverified，复用更严格风险与只上传允许字段。
- **11-W**：普通执行可无限等工具，超时后还可能继续后续节点；活动包切换时健康证据错配；回滚恢复只检查执行成功不核对期望/真实切换；槽标记与正文可指向不同制品；健康结果进入队列前校验仍有竞态。现工具等待硬上界且超时不重试/不调度下游，执行固定快照，健康/恢复核对release/version/digest和期望，更新管理器串行区内再次核验来源。
- 仍不能强杀已下发的任意Promise工具；超时表达未知、不晋级健康，不谎称外部动作已取消。设备去重锁范围为同JS进程共享KV对象/key，跨进程依赖已持久化提议；不能宣称跨进程CAS。紧凑去重标记单独保留，生产归档/清除需另设计，不能为容量静默丢弃未知副作用。

## 证据与计数

- 固定修复 **{commit}** 的Git归档：后端 **1661 passed / 1 skipped**；客户端 **228 passed**；TypeScript通过；声明/台账/认领/缺口/89主题覆盖五项检查通过。线程异常警告升级错误。依赖复用本机node_modules，不宣称空机安装验收。[固定源码验证](../../../{prefix}/committed-evidence.json)。
- 新增Runtime8项、客户端21项；最终探针注入原08d2ac9归档为 **Runtime7失败/1通过、客户端21失败/0通过**。真实沙箱联网拒绝是原版已通过的正向控制；原207项客户端与原Runtime8项测试未改。[原始复现与探针哈希](../../../{prefix}/before-evidence.json)。
- [真机证据交叉审计](../../../{prefix}/device-evidence-audit.json)：读取zcode-main采集的截图/XML、演示服务数据库与设备KV快照。批准及断网恢复任务SUCCEEDED，拒绝CANCELLED；过期SUSPENDED，SQL原pending但ApprovalStore按有效期派生expired；离线dump中的批准/拒绝按钮enabled=false。11-W的active、last_known_good、recovered同为rel_good_v3/v9.2.0/8e5212ed…，failed为rel_bad_v4/v9.3.0/47c6…并有实际恢复执行ID。
- 采集归属和环境见[04-UI四路径](2026-10-24-ALN-04-UI真机四路径证据.md)、[11-W健康回滚](2026-10-24-ALN-11-W真机健康回滚证据.md)。本会话已视觉查看真实截图并核对原始状态，**没有声称这些场景由本会话重新采集**；截图与KV也不代表07466fb已经重装到手机。新修复由固定源码回归验证，未采集新的物理重启或生产供应商证据。

## 流程与基线

- 五卡reviewer改为实际复验人codex-runtime，原reviewer及结论进入review_history，旧第三方记录不覆盖。04-UI作者同为codex-runtime，台账工具仅在显式user_request授权、allow_same_author=true、independent=false且有报告时允许；默认自审拒绝保留，并有负向校验。
- 最新prepared_candidate已由其他会话更新为**07f11e5**（旧22c50ca候选保留历史）。它的1653/1+207证据不含本轮07466fb修复，不能直接发布。00-B继续blocked，需刷新选定候选与干净证据再走发布；published_commit仍空，未把“所有卡已验收”当发布授权。
- 未纳入通知、backgroundRunner、DevicePackage及历史日志在途，未自改原native权限。父工作包仍按正式设计差距推进。
"""
(root / report).write_text(report_text, encoding="utf-8", newline="\n")

def edit(relative, fn):
    path = root / relative; path.write_text(fn(path.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")

note = "2026-10-24最新验收：**按用户明确授权，由codex-runtime统一复验通过既定切片（accepted）**，自己的修复不伪称第三方；范围、修复、真机证据归属和父包后续见[统一验收](../验收/2026-10-24-全部待审核统一验收.md)。下文保留原交付/任务时点。"
for card in board["dispatches"]:
    if card["id"] not in ids: continue
    for relative in [card["card"], card.get("delivery")]:
        if not relative or not (root / relative).exists(): continue
        edit(relative, lambda t: t.split("\n",1)[0] + "\n\n" + note + "\n" + t.split("\n",1)[1])

readme = root / "docs/协作开发/README.md"
t = readme.read_text(encoding="utf-8")
title = t.split("## 接手与共享文件",1)[1]
t = f"""# 协作开发入口

更新时间2026-10-24；结构化状态以[workboard.json](workboard.json)为准，[认领表](开发任务认领表.md)由工具生成。

当前41张子卡为**40 accepted、0 review、1 blocked**。按用户明确要求，本会话已统一复验04-UI、07-B、11-W、12-A、20-A并验收既定切片；原作者/旧独立复核和本轮非独立复验区分记录，见[统一验收](验收/2026-10-24-全部待审核统一验收.md)。

## 当前安排

初始开发队列四卡已交付；没有待审子卡。00-B仍blocked：已准备的07f11e5候选不含07466fb新修复，需要刷新选定候选和干净验证后再发布。candidate、published_commit为空不变。

后续实施重点仍是父包缺口：自然语言手机App主链/真机全矩阵、精确步骤授权与原子绑定/对账消费、固定外部Runtime PoC，以及生产签名/价格/多端与组合恢复。子卡接受不代表23个父包全量完成，见[当前设计差距](../项目现状与审阅进展_2026-10-24.md)。

## 代码与发布现状

- 固定07466fb归档：后端**1661通过/1跳过**、客户端**228通过**、typecheck及五项元数据/覆盖检查通过。[证据](../../{prefix}/committed-evidence.json)。
- 04-UI批准/拒绝/断网/过期、11-W真实回滚/旧包执行/重启证据已交叉核对，采集人为zcode-main；新修复未重装到手机，本轮以固定源码回归覆盖。
- 12-A接受的是正式任务卡首份协议/矩阵；自然语言执行全链与物理重启矩阵仍待实施。20-A接受协议与兼容矩阵，真实固定外部PoC仍待实测。
- 07f11e5候选1653/1+207与更早22c50ca/aafca22/cc0aa3a验证均保留各自时点；本轮没有发布。

## 接手与共享文件""" + title
readme.write_text(t, encoding="utf-8", newline="\n")
edit("docs/项目现状与审阅进展_2026-10-24.md", lambda t: t.replace(
    "当前有**41张子卡：35 accepted、5 review、1 blocked**，无claimed/in_progress。", "当前有**41张子卡：40 accepted、0 review、1 blocked**，无claimed/in_progress。")
    .replace("prepared_candidate仍为历史22c50ca", "prepared_candidate为07f11e5（尚不含07466fb新修复）")
    .replace("固定最新`cc0aa3a`归档验证：后端**1653通过/1跳过**，本轮相关241通过；五项元数据/覆盖检查通过。客户端在同归档实际复跑207通过及类型检查，未重跑真机。[验证记录](../.planning/2026-10-24-action-binding-review/committed-evidence.json)。07-B六组问题已修复待独立复核，[审核记录](协作开发/验收/2026-10-24-ALN-07-B独立审核.md)。旧结果保留历史。",
             f"固定最新07466fb归档：后端**1661通过/1跳过**、客户端**228通过**、typecheck及五检查通过。[验证记录](../{prefix}/committed-evidence.json)。五张待审卡已按用户要求统一复验接受既定切片；真机为已有取证的交叉复核，不冒称新修复已重装。[统一验收](协作开发/验收/2026-10-24-全部待审核统一验收.md)。")
    .replace("07-P/W/T/R/E接受；07-B六组问题审核修复待复核", "07-P/W/T/R/E/B既定切片均接受")
    .replace("04-UI单元接受、真机待验", "04-UI四路径证据已复核接受")
    .replace("批准/拒绝/离线/过期四路径及组合恢复", "生产部署与组合恢复矩阵")
    .replace("11-W自动化切片已复核、真机待验", "11-W切片及既有真机回滚证据已接受")
    .replace("12-A协议/离线自动化已复核，保持review/device_pending", "12-A协议/矩阵切片已接受，父包真机全链另验")
    .replace("20-A协议/Mock与现有适配/矩阵已交付review", "20-A协议/Mock与现有适配/矩阵已接受")
    .split("## 下一步\n",1)[0] + "## 下一步\n\n1. 00-B刷新包含07466fb修复的候选、核对干净证据并走发布流程；不发布旧07f11e5。\n2. 后续开发按父包缺口拆卡：手机自然语言主链、精确动作绑定/授权/对账消费、固定外部Runtime PoC和生产环境矩阵。\n3. 当前无待审子卡；既定切片验收与整包全量完成、发布分开记录。\n\n结构化状态见[workboard.json](协作开发/workboard.json)。\n")
edit("docs/项目现状与设计差距核对_2026-10-23.md", lambda t: t.splitlines()[0] + "\n\n最新状态见[2026-10-24审阅进展](项目现状与审阅进展_2026-10-24.md)：41张子卡40 accepted/0 review/1 blocked；五张原待审已统一复验验收，固定07466fb为1661/1、客户端228/typecheck。**下文为历史盘点，不用于读取当前状态。**\n" + "\n".join(t.splitlines()[3:]) + "\n")
for name in ("progress.md", "findings.md"):
    with (folder/name).open("a", encoding="utf-8", newline="\n") as out:
        out.write("\n- 固定07466fb全量1661/1、客户端228、typecheck和五检查通过；五卡按用户授权完成复验并接受其既定切片，自己的修复不伪称第三方。新eaa1342准备候选07f11e5保留，但不含07466fb修复，00-B仍需刷新候选，不发布。\n")
path = folder / "task_plan.md"
t = path.read_text(encoding="utf-8").replace("|逐卡代码/契约与原证据核对，检查真机连接|in_progress|", "|逐卡代码/契约与原证据核对，检查真机连接|complete|")
t = t.replace("|独立负向与正向探针，复现并修复|pending|", "|独立负向与正向探针，复现并修复|complete|")
t = t.replace("|固定提交全量与实际设备可达范围验证|pending|", "|固定提交全量与实际设备可达范围验证|complete|")
t = t.replace("|逐卡验收结论、剩余缺证、台账提交|pending|", "|逐卡验收结论、剩余缺证、台账提交|complete|")
path.write_text(t, encoding="utf-8", newline="\n")
print("accepted:", ids, "source", commit)
