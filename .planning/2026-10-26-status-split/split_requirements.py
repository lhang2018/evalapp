"""Extend the registered plan without reassigning existing work or acceptance."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PLAN = "docs/协作开发/需求拆分与分期实施_2026-10-25.json"
BOARD = "docs/协作开发/workboard.json"
ROUND = "STATUS-SPLIT-2026-10-26"
REPORT = "docs/协作开发/新增状态复核与需求拆分_2026-10-26.md"
SOURCE = "0e1b4fd61c9f616f9f9fe560b39f3d759d7437cd"


def item(id, title, priority, phase, deps, refs, paths, goal, checks, prerequisite, zg, cases=()):
    return dict(id=id, workpackage=id.rsplit("-", 1)[0], title=title,
                priority=priority, phase=phase, depends_on=deps.split(),
                requirement_refs=refs.split(), write_paths=paths,
                goal=goal, checks=checks, acceptance="；".join(checks),
                prerequisites=prerequisite, backlog_refs=zg.split(),
                cases=list(cases), split_round=ROUND)


LOCAL = "可先做本地组件与拒绝路径；正式集成须核对depends_on的实际交付，未验收不冒称通过"
specs = [
 item("ALN-30-B", "客户端工作区成果与记忆控制接线", "P0", 1,
      "ALN-23-C ALN-24-A ALN-24-C ALN-24-D", "UX-07 UX-08 UX-11 UX-12 UX-13 UX-14 UX-17 UX-24",
      ["android-app/src/features/workspace/", "android-app/src/features/artifacts/", "android-app/src/features/projects/", "android-app/src/features/memory/", "android-app/App.tsx", "android-app/src/api.ts"],
      "把既有工作区、成果版本/验收、项目延续和记忆管理组件接到真实App入口；手机执行仍沿12-E，不复制任务权威。",
      ["真实任务的计划/成果/验收状态可从页面追溯，下载或打开真实文件，空或失败结果不显示已交付",
       "项目切换不串任务；记忆纠正、禁用、删除调真实API并展示保留边界，禁用后后续模型不再收到该范围内容",
       "覆盖离线/超时/取消/权限拒绝；固定APK/JS实测与组件证据分别记录，未连设备不称真机通过"],
      LOCAL + "；设备验收需明确空闲窗口", "ZG-03 ZG-05 ZG-10", ["UX-F02-01", "UX-F02-03", "UX-GLOBAL-02"]),
 item("ALN-16-Q", "聚合观测与受控导出删除API接线", "P0", 2,
      "ALN-16-P ALN-16-X", "UX-17 UX-24 G2",
      ["evoagent/api/evidence_api.py", "evoagent/api/routes.py", "evoagent/observability/repository.py", "evoagent/observability/export.py"],
      "为既有聚合证据查询、授权导出下载及删除执行器提供受鉴权的API消费者；复用审计、保留项和受信身份，不重做仓储。",
      ["真实HTTP查询任务/运行证据，跨主体及越权导出删除拒绝，不能从body伪造principal",
       "下载字节与审计/摘要匹配；删除逐层返回实际计数、残留和失败，不宣称跨备份或全域擦除",
       "聚合观测与既有feedback入口分开审计，证明端点已注册并执行服务，异常不返回假成功"], LOCAL, "ZG-10"),
 item("ALN-29-F", "真实成果纵切片与同版本场景采证", "P0", 1,
      "ALN-29-A ALN-29-B ALN-23-A ALN-23-C ALN-24-A ALN-23-E ALN-30-B", "UX-01 UX-07 UX-10 UX-11 UX-12 F01 F02 F03 G0 G1 G3 G7",
      ["tests/product_scenarios/", "tools/product_acceptance.py", "docs/协作开发/验收/首发验收矩阵.json", "docs/协作开发/验收/首发门禁证据_2026-10-25.json"],
      "在同一固定版本打通资料输入到真实报告文件、独立验收、用户可打开成果，并分批采集首发场景证据。",
      ["首条报告任务使用实际配置模型和真实资料/工具产生文件，记录task/run/model/tool/版本、产物与独立验收结果",
       "实际执行工具失败、验收失败、取消、预算耗尽和恢复分支；保留失败项及未运行项，不从组件日志拼observations",
       "场景分别绑定源码/APK/JS与实际环境；只更新本次真实执行的用例/门禁，F04及其他未执行项仍not_run/untested"],
      "真实模型服务、明确费用上限与测试资料；App真实交付与打开验证须设备环境，未具备时先完善可复跑驱动", "ZG-05",
      ["UX-F01-01", "UX-F01-02", "UX-F02-01", "UX-F02-02", "UX-F02-03", "UX-F03-03", "UX-F03-04", "UX-GLOBAL-03"]),
 item("ALN-15-E", "原生资源采样与后台周期准入", "P1", 3,
      "ALN-15-D ALN-25-B", "UX-21 G4 G5",
      ["android-app/android/app/src/main/java/", "android-app/src/device/", "android-app/src/offline/backgroundRunner.ts", "android-app/src/offline/backgroundNative.ts"],
      "提供有时间和有效性标记的原生CPU/内存/电量/磁盘采样，把后台周期工作接到既有准入与暂停策略；不复制预算/阈值权威。",
      ["实际原生采样带来源、单位、采样时间与不可用状态，缺失/过期不伪造低压",
       "高压或should_pause实际阻止新增后台动作，已有副作用先对账；采样失败不改前台权威任务",
       "固定包测周期触发、后台/重启及资源压力；夹具仅验证分支，真实非干扰指标单独留证"], LOCAL + "；最终需Android硬件与压力测量", "ZG-04 ZG-08" , ["UX-F04-03"]),
 item("ALN-07-X", "恢复策略运行时执行与审批衔接", "P1", 2,
      "ALN-07-R ALN-07-C ALN-04-R", "UX-09 UX-10 F03",
      ["evoagent/workflows/recovery_executor.py", "evoagent/core/controller.py", "evoagent/planning/planner.py", "evoagent/workflows/engine.py"],
      "将已有恢复策略声明接到实际执行器，支持require_approval和partial_retry；fallback_tool/checkpoint仅对有实际产出和证据的意图开放。",
      ["失败后按策略产生实际审批/恢复动作，拒绝或过期不执行；计划修订、步骤和effect保持绑定",
       "已确认成功副作用不重放，unknown/submitted先对账；部分重试只作用受影响步骤，崩溃后幂等恢复",
       "无可执行策略或旧格式诚实停止/提示，不能把风险文案当恢复成功"], LOCAL, "ZG-06", ["UX-F02-02", "UX-F03-04", "UX-F03-05"]),
 item("ALN-13-S", "长期目标调度启动与持久周期消费", "P1", 3,
      "ALN-13-P ALN-15-P", "F04 B-90 B-91",
      ["evoagent/goals/scheduler.py", "evoagent/app.py", "evoagent/cli.py", "evoagent/api/goals_api.py"],
      "把GoalScheduler的实际tick、持久周期和启停挂到宿主；已有GoalGovernor.advance和问题/机会接口保留，不重做。",
      ["真实启停后按持久到期时间推进一次任务，多实例或重启不重复派发",
       "撤销授权、预算不足、主动关闭和资源让出时不启动；inflight失败/unknown按账本处理",
       "CLI/API可观测周期、延迟、失败和恢复，宿主退出可停止且不遗留失控循环"], LOCAL, "ZG-01 ZG-08", ["UX-F04-01", "UX-F04-04"]),
 item("ALN-02-I", "供应商动作工具与主链副作用接线", "P1", 2,
      "ALN-02-P ALN-01-A ALN-07-C", "UX-10 F03 G2",
      ["evoagent/tools/vendor_http.py", "evoagent/tools/registry.py", "evoagent/app.py", "evoagent/config.py"],
      "把既有vendor.submit适配器通过显式配置和工具注册接入真实任务，复用动作授权、幂等键及供应商回执对账。",
      ["未配置/未授权时明确不可用；已配置任务实际调用受控接收端并绑定effect/step，不接受自述成功",
       "提交后超时/进程崩溃先查回执，拒绝盲目重发；恢复对账与补偿按原契约验证",
       "受控本地服务与真实供应商分开取证，凭据不写事件/日志，真实费用与环境未验时保留未验"],
      LOCAL + "；实际供应商联测需服务配置和预算", "ZG-01 ZG-11", ["UX-F03-05"]),
 item("ALN-09-I", "真实候选宿主消费与队列管理入口", "P1", 4,
      "ALN-09-H ALN-10-E", "B-78 G2 G3",
      ["evoagent/integration/candidate_execution.py", "evoagent/evolution/controller.py", "evoagent/api/candidate_host_api.py", "evoagent/api/routes.py", "evoagent/app.py"],
      "将IsolatedCandidateHost/HostBenchmark接到受控候选评测入口，补现有ProblemQueue的查询/管理面；持久队列已经被进化控制器消费，不重建。",
      ["只消费通过摘要/版本验证的真实候选制品，结果绑定基线、候选、数据集及实际宿主",
       "队列查看/重试遵守权限和租约，进程失败不重复提案或绕过独立门禁",
       "超时/异常进程终止、隔离和证据落盘可验证；评测成功不自动发布，不将返回字符串当候选收益"], LOCAL, "ZG-01 ZG-11"),
 item("ALN-18-H", "编程专家按项目安装与真实工具消费", "P1", 2,
      "ALN-18-G ALN-06-R ALN-23-A", "F02 A-60",
      ["evoagent/experts/coding_consumer.py", "evoagent/app.py", "evoagent/core/controller.py", "evoagent/tools/registry.py"],
      "按受信target_object/project root为编程专家装配既有精确版本编码包，避免全局注册表跨项目覆盖。",
      ["从实际任务选中专家到受限根安装再执行真实编辑/语法与项目测试，记录执行命令/returncode和磁盘变化",
       "跨根、链接替换、包版本不符和未知工具拒绝；同根幂等，不同根独立注册表",
       "失败后实际回滚并重新执行检查；不自动安装外部依赖、提交代码或扩大授权"], LOCAL + "；选定临时真实项目与固定测试依赖", "ZG-07", ["UX-F02-01", "UX-F02-02"]),
 item("ALN-25-C", "主动任务准入执行与真实资源非干扰", "P1", 3,
      "ALN-25-A ALN-25-B ALN-15-D ALN-30-A", "UX-21 F04 G5",
      ["evoagent/goals/proactive_execution.py", "evoagent/goals/proactivity.py", "evoagent/core/controller.py", "evoagent/app.py"],
      "将may_start/defer/stop与实际主动任务启动、原子预算预留和停止挂接，复用30-A只读分析入口。",
      ["defer/关闭/无高价值机会时实际零新增非必要任务与外部动作，未启动预留可释放",
       "高压或无进展停止可观测，核心任务状态和已完成结果不被改写，unknown成本不按零处理",
       "在真实宿主负载记录核心任务质量/延迟/资源阈值和主动让出，模拟预算计数不替代实测"], LOCAL + "；最终需固定宿主真实压力数据", "ZG-08", ["UX-F04-02", "UX-F04-03", "UX-F04-04"]),
 item("ALN-26-C", "能力缺口遥测预算与用户可见计划", "P1", 3,
      "ALN-26-A ALN-15-P ALN-30-B", "A-59 UX-22 UX-25",
      ["evoagent/goals/gap_planner.py", "evoagent/api/gap_planner_api.py", "evoagent/api/routes.py", "android-app/src/features/gaps/", "android-app/App.tsx"],
      "把已实现缺口规划器接实际运行/反馈证据和真实预算台账，通过API与App展示来源、估算、限制及候选去向。",
      ["接收的运行/反馈可追溯且过期/伪造/跨领域证据不晋升，同一事件不重复计阈值",
       "计划与原子预算/unknown挂账对齐，候选在权威仓储可查且保留requires_evaluation",
       "用户看到的证据、费用未知与收益未证实均如实，API拒绝越权，不自动发布"], LOCAL, "ZG-08"),
 item("ALN-26-D", "动态团队与组合工作流真实执行", "P1", 4,
      "ALN-26-B ALN-27-B ALN-07-C ALN-15-P", "A-53 A-60 A-70",
      ["evoagent/experts/team_execution.py", "evoagent/assets/composition.py", "evoagent/workflows/engine.py", "evoagent/core/controller.py"],
      "将冻结团队契约和精确版本组合资产消费为实际工作流，执行角色交接、输入输出和权限/预算边界。",
      ["真实任务调用各角色工具，交接产物由实际输出产生并通过schema/版本/依赖校验",
       "角色冲突、预算不足、未知副作用和取消时停止/对账，不能仅修改契约状态冒充执行",
       "动态展开保持版本固定、可恢复和审计；已完成节点不重放，原团队/组合仓储继续作为权威"], LOCAL, "ZG-08 ZG-09"),
 item("ALN-27-C", "知识持久化真实注入与迁移质量验证", "P1", 4,
      "ALN-27-A ALN-30-A ALN-05-I", "A-69 B-81 B-88",
      ["evoagent/knowledge/store.py", "evoagent/knowledge/applicability.py", "evoagent/knowledge/transfer.py", "evoagent/memory/context.py", "evoagent/app.py"],
      "将30-A进程内知识库扩为可重启的受控仓储，plan_injection接实际上下文，迁移验证消费真实前后任务数据。",
      ["实际任务能使用适用知识并在manifest留来源/版本，撤销/低可信/不适用条目不进入模型",
       "重启保留版本和撤销，跨用户/项目可见性沿既有身份；旧纯内存使用方式兼容",
       "相同固定任务集前后质量、退化与限制留证，命中率或手填分数不能替代迁移收益"], LOCAL + "；需固定前后任务集与运行版本", "ZG-09"),
 item("ALN-16-E", "端侧证据上传与业务缓存治理接线", "P1", 3,
      "ALN-16-P ALN-16-Q ALN-12-A ALN-30-B", "UX-17 UX-24 G2",
      ["android-app/src/offline/", "android-app/src/observability/", "evoagent/observability/repository.py", "evoagent/api/events_sync.py"],
      "把已有EvidenceEvent投影接真实端侧事件队列和聚合查询，登记实际业务缓存的证据/删除适配，不复制正文或凭据。",
      ["实际设备事件经队列去重上传并关联task/run/origin，未验证来源不能自标trusted",
       "restricted正文不进入上传负载，断网重启保留待传状态；缓存删除/保留逐层可核对",
       "撤销/删除后的端侧缓存失效有真实回执，未覆盖的外部层明确保留；不承诺全域擦除"], LOCAL + "；最终端侧证据需设备", "ZG-10"),
 item("ALN-18-S", "真实语言服务接入与语义改写", "P2", 4,
      "ALN-18-L ALN-18-H", "A-60 A-70 F02",
      ["evoagent/tools/language_service.py", "evoagent/tools/refactor.py", "evoagent/experts/coding_consumer.py"],
      "在现有AST诚实降级基础上接入固定版本真实语言服务，按能力声明支持具体语言与跨文件语义改写。",
      ["记录服务版本/语言/能力并实际请求定义、引用和重命名，不把AST结果标成LSP",
       "多文件改写后运行真实编译/项目测试；歧义、过期版本、越根与服务不可用时不产危险计划",
       "保留dry-run、原子补丁和回滚边界；未支持语言明确不可用，不扩大写盘授权"], "需本地固定语言服务及项目依赖，可先实现契约/拒绝路径", "ZG-07"),
 item("ALN-20-R", "固定外部Runtime的任务主链采用", "P2", 4,
      "ALN-20-P ALN-18-H ALN-15-P", "B-75 G2 G3",
      ["evoagent/runtime/adapter.py", "evoagent/runtime/external_poc.py", "evoagent/core/controller.py", "evoagent/app.py", "evoagent/config.py"],
      "在已有真实Ollama PoC之上增加显式任务级采用入口，验证工具循环、取消、恢复、预算及授权的等价边界。",
      ["按固定Runtime/模型摘要执行真实任务并记录实际采用，拒绝版本漂移或能力声明不足",
       "外部事件不能冒充本系统run/effect权威，取消/异常/重启后不重放未知副作用",
       "记录并发和长时稳定性、实际资源/费用未知项；PoC成功不自动激活生产默认"], "需已固定外部Runtime、本地模型和受控资源预算", "ZG-11"),
 item("ALN-24-F", "多主体记忆范围策略与迁移隔离", "P2", 4,
      "ALN-24-D ALN-05-I", "UX-17 UX-24 GLOBAL",
      ["evoagent/memory/scope_policy.py", "evoagent/memory/user_control.py", "evoagent/memory/store.py", "evoagent/api/memory_context_api.py"],
      "作为多主体开放前置，将现有本地实例单行策略扩为受信主体分区，定义旧策略迁移与租户隔离；不重复c6c9872检索过滤。",
      ["主体由服务端鉴权确定，不能通过body选择他人策略；不同用户同scope互不影响",
       "旧单用户策略迁移/回退可核对，缺失或损坏策略仍按明确规则拒绝/默认而不跨主体放宽",
       "实际API到模型消息和manifest验证分区/Grant/撤销/重启；App管理消费与30-B沿同一接口"], "只在明确启用多主体场景前实施；当前单用户过滤修复单独复核", "ZG-02 ZG-10"),
]

board_path, plan_path = ROOT / BOARD, ROOT / PLAN
board = json.loads(board_path.read_text(encoding="utf-8"))
plan = json.loads(plan_path.read_text(encoding="utf-8"))
before = {row["id"]: (row.get("owner"), row.get("reviewer"), row.get("status")) for row in board["dispatches"]}
shared = {row["path"] for row in board["shared_files"]}
plan_ids = {row["id"] for row in plan["tasks"]}
registered = {row["id"] for row in board["dispatches"]}

for spec in specs:
    identity = spec["id"]
    spec["stream"] = ("android" if identity in {"ALN-30-B", "ALN-15-E", "ALN-16-E"} else
                      "integration" if identity in {"ALN-16-Q", "ALN-29-F", "ALN-24-F"} else
                      "assets" if identity in {"ALN-09-I", "ALN-27-C"} else "runtime")
    card = f"docs/协作开发/任务卡/{identity.removeprefix('ALN-')}-{spec['title']}.md"
    spec["card"] = card
    changes = [path for path in spec["write_paths"] if path in shared]
    task = {key: value for key, value in spec.items() if key not in {"checks", "cases", "card"}}
    if identity not in plan_ids:
        plan["tasks"].append(task)
    if identity not in registered:
        board["dispatches"].append(dict(id=identity, workpackage=spec["workpackage"], title=spec["title"],
            stream=spec["stream"], owner=None, reviewer=None, branch=None, base_commit=None,
            status="unclaimed", blocker=None, evidence=[], card=card, shared_files=changes,
            requirement_batch=plan["id"], phase=spec["phase"], depends_on=spec["depends_on"],
            requirement_refs=spec["requirement_refs"], priority=spec["priority"],
            next_step="按本卡来源、依赖与写入边界认领；已交付待审接口可用于准备，正式验收须实际消费证据",
            working_basis=f"用户要求核对新增状态并拆需求；{SOURCE}为本次拆分来源锚点，尚未认领，不预填开发base_commit",
            prerequisites=spec["prerequisites"], split_round=ROUND, backlog_refs=spec["backlog_refs"]))
    for ref in spec["requirement_refs"]:
        if ref in plan["ux_coverage"] and identity not in plan["ux_coverage"][ref]:
            plan["ux_coverage"][ref].append(identity)
    for case in spec["cases"]:
        if identity not in plan["acceptance_coverage"][case]:
            plan["acceptance_coverage"][case].append(identity)
    text = f"# {identity}｜{spec['title']}\n\n优先级：{spec['priority']}；阶段：{spec['phase']}；建议开发线：{spec['stream']}。\n状态：unclaimed；owner/reviewer/branch/base_commit均以workboard实际认领为准。\n来源：本轮{ROUND}，关联{' / '.join(spec['backlog_refs'])}；[待接清单](../待接需求_2026-10-26.md)、[本轮状态与拆分](../新增状态复核与需求拆分_2026-10-26.md)。\n需求：{' / '.join(spec['requirement_refs'])}。\n\n## 目标\n\n{spec['goal']}\n\n## 依赖与开工条件\n\n- depends_on：{', '.join(spec['depends_on'])}\n- 环境：{spec['prerequisites']}\n- 已有切片复用；开发可用已交付待审接口做独立准备，正式接受需实际集成与依赖核对。未就绪条件不能靠Mock宣称关闭。\n\n## 写入边界\n\n"
    text += "\n".join("- " + path for path in spec["write_paths"])
    text += "\n\n共享文件精确接线按workboard.shared_files串行整合；认领前检查他人在途修改。同一App.tsx/app.py/controller.py修改不得并发覆盖，独占模块可先开发。不新建第二套任务、身份、预算或副作用权威。\n\n## 验收\n\n"
    text += "\n".join(f"{i}. {check}" for i, check in enumerate(spec["checks"], 1))
    text += "\n4. 给出正常、失败、拒绝/恢复路径的精确源码、命令、日志及产物；组件、集成、设备和生产证据分别记录。场景未跑保持未测，开发完成不替代独立接受或发布。\n\n## 交付\n\n交付文档写清触发前后行为、复用与新增、迁移/兼容、验证和保留缺口；提交后填写40位delivered_commit并转review。owner/reviewer未分配保持null，不自批；若扩大边界，先同步任务卡与分期清单。\n"
    (ROOT / card).write_text(text, encoding="utf-8")

for path in (REPORT, "docs/协作开发/待接需求_2026-10-26.md", "docs/zcode遗留与现状对账_2026-10-26.md"):
    if path not in plan["sources"]:
        plan["sources"].append(path)
history = plan.setdefault("change_history", [])
if not any(row.get("id") == ROUND for row in history):
    history.append(dict(id=ROUND, date="2026-10-26", source_commit=SOURCE, authorized_by="user_request",
                        request="梳理新增状态，检查是否正确，然后拆需求", tasks=[row["id"] for row in specs],
                        note="17张未认领卡；不改原包标题/依赖/关闭标准，不重复已有修复或设备验收卡"))

card = next(row for row in board["dispatches"] if row["id"] == "ALN-30-A")
assert card["status"] == "review"
card["delivered_commit"] = "67bed4644a84fd564a0e752ac4bc06b4f7ba3382"
card["next_step"] = "交付67bed46维持review，完整delivered_commit已补；本轮固定0e1b4fd相关176过，非完整独立接受。旧2299/1日志未绑定精确树，当前tests采集2328项，仍需当前版本完整取证；聚合观测API/知识注入/主动执行等由新卡承接。"
card["status_check"] = dict(date="2026-10-26", source_commit=SOURCE, report=REPORT,
    scope="Git/实际入口/固定176项接续核对；状态不升accepted，原作者和审阅归属保留")
if not any(isinstance(row, dict) and row.get("status_split_round") == ROUND for row in card["evidence"]):
    card["evidence"].append(dict(date="2026-10-26", status_split_round=ROUND, source_commit=SOURCE,
        result="固定相关176过；当前tests仅采集2328，非全量运行。区分六项声明可达与全域消费，交付保持review",
        log=".planning/2026-10-26-status-split/runtime-validation.json", delivery=REPORT))
next(row for row in board["workpackages"] if row["id"] == "ALN-30")["next_step"] = "30-A已交付review（67bed46），待补精确全量/接受证据；30-B客户端接线已拆未认领；原包关闭标准与发布边界不变"
anchor = "67bed4644a84fd564a0e752ac4bc06b4f7ba3382"
change_id = "discovered-" + anchor[:12]
if not any(row["id"] == change_id for row in board["post_candidate_changes"]):
    board["post_candidate_changes"].append(dict(id=change_id, title="feat(ALN-30-A): 服务端装配收口与能力可达性审计",
        workpackage="ALN-30", source_commit=anchor, status="source_registered_pending_validation",
        files=["evoagent/app.py", "evoagent/cli.py", "evoagent/integration/__init__.py",
               "evoagent/integration/services.py", "evoagent/integration/wiring.py",
               "tests/test_integration_wiring.py", "tests/test_update_artifact_delivery.py"],
        candidate_included=False, independent_review=False, report=REPORT,
        scope="补漏登记七个源码文件；固定0e1b4fd相关176通过，原历史2299/1不绑定当前树，保持review未发布"))
board["execution_note"] = "2026-10-26核对新增交付：30-A已交付review；本轮补拆17张未认领卡，合计31包105卡。原卡owner/reviewer/status保留，当前无单会话开发队列。按实际认领单人WIP=1，共享文件串行；待审接口可用于独立准备，不等于接受或发布。共同基线21899e6已发布，后续增量及首发产品门禁另验。"
board["updated_at"] = "2026-10-26"
for identity, original in before.items():
    row = next(row for row in board["dispatches"] if row["id"] == identity)
    assert (row.get("owner"), row.get("reviewer"), row.get("status")) == original

plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2)+"\n",encoding="utf-8")
board_path.write_text(json.dumps(board,ensure_ascii=False,indent=1)+"\n",encoding="utf-8")
summary = dict(source_commit=SOURCE, added=len(specs), priorities=dict(Counter(row["priority"] for row in specs)),
               before_cards=len(before), after_cards=len(board["dispatches"]), states=dict(Counter(row["status"] for row in board["dispatches"])),
               workpackages=len(board["workpackages"]), original_assignments_preserved=True,
               source_sha256_lf={path:hashlib.sha256((ROOT/path).read_bytes().replace(b"\r\n",b"\n")).hexdigest() for path in (PLAN,BOARD)},
               tasks=[{key:row[key] for key in ("id","title","priority","phase","depends_on","card","backlog_refs")} for row in specs])
(OUT/"split-summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
print(json.dumps({key:summary[key] for key in ("added","priorities","after_cards","states","workpackages")},ensure_ascii=False))
