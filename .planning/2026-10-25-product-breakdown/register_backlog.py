"""One-time registration of the user-authorized product backlog."""
import json
from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parents[2]
board_path = root / "docs/协作开发/workboard.json"
board = json.loads(board_path.read_text(encoding="utf-8"))
head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=root, text=True).strip()
assert head == "5e2b7e36ffbc225a33c7c09c8214bf70133ba2da"
assert not any(p["id"] == "ALN-23" for p in board["workpackages"])
plan_relative = "docs/协作开发/需求拆分与分期实施_2026-10-25.json"
source = ["chat/extracted/156_qa.md", "chat/extracted/157_qa.md", "chat/extracted/154_qa.md",
          "docs/README新增设计梳理与实施映射_2026-10-25.md",
          "docs/全量对齐与下一步实施计划_2026-10-22.md"]
package_specs = [
    (23, "P0", "任务入口、聊天/工作模式与任务工作区", [4,7,12], "android", "F01/F02/F03真实入口、计划、控制与恢复闭环；UX01—10逐项验收"),
    (24, "P0", "成果交付、项目记忆与能力信任中心", [1,3,5,6,23], "android", "真实产物可访问和继续修改；项目/记忆/能力/权限/隐私/成本按UX11—17、22—25逐项验收"),
    (25, "P1", "主动帮助与机会发现", [5,13,14,15,23], "models", "授权来源到建议的可追溯闭环；F04全通过，含关闭和资源非干扰实测"),
    (26, "P1", "能力缺口主动规划与动态编排", [6,7,9,10], "runtime", "A-59/A-60从缺口证据到候选计划、动态团队契约与冲突处理有实际任务证据"),
    (27, "P1", "经验迁移与可组合能力资产", [5,6,10], "assets", "A-69/A-70来源、适用域、撤销与跨专家迁移/组合工作流在真实消费路径有效"),
    (28, "P2", "外部事实核验与可信交付", [5,9,16], "models", "A-61外部证据获取、交叉核验、矛盾与未知标注可复现；外部数据源未就绪不宣称通过"),
    (29, "P0", "首发范围、发布门禁与用户反馈闭环", [0,10,11,16,23,24,25], "integration", "28UX范围表、17用例及G0—G7同版本证据齐备；发布决定独立于开发完成"),
]
packages = []
for number, priority, title, deps, stream, close in package_specs:
    packages.append(dict(id=f"ALN-{number:02}", priority=priority, title=title,
        depends_on=[f"ALN-{d:02}" for d in deps], stream=stream, owner=None,
        delivery_state="planned", next_step="按分期任务卡实施；尚无整包验收结论",
        requirement_batch="PRODUCT-2026-10-25", closure_criteria=close,
        source_refs=source[:4]))

specs = []
def card(identity, title, stream, phase, deps, refs, paths, goal, acceptance, prerequisites="仅需仓库与本地测试环境"):
    specs.append(dict(id="ALN-"+identity, workpackage="ALN-"+identity.split("-")[0],
        title=title, stream=stream, phase=phase, depends_on=["ALN-"+x for x in deps],
        requirement_refs=refs.split(), write_paths=paths.split(), goal=goal,
        acceptance=acceptance, prerequisites=prerequisites))

# Phase 0: acceptance foundations. Only 29-A is active; 23-A is claimed next.
card("29-A","首发17用例的验收骨架与可执行断言","integration",0,[], "F01 F02 F03 F04 GLOBAL",
     "tools/check_collaboration_plan.py tools/check_product_acceptance.py tools/product_acceptance.py tools/tests/ docs/协作开发/验收/首发验收矩阵.json",
     "把原文17用例落为可执行的事实断言、证据格式和未测试状态，供后续实现共同使用",
     "17项编号/场景/条件与原文一致；无证据全部未测试；缺断言、错误版本、篡改证据、字段缺失不得通过；夹具不算真实验收")
card("29-B","首发冻结范围与八道发布门禁","integration",0,["29-A"],"UX-26 UX-27 UX-28 G0 G1 G2 G3 G4 G5 G6 G7",
     "tools/ docs/协作开发/验收/",
     "把EA-REL-005的范围与G0—G7编成版本绑定证据表，明确命令/人工评估/真机条件",
     "八项逐项可复核；未运行不记通过；结构验证与功能/真机/发布决定分离；无完整证据不开放生产")
# Phase 1: one task authority and a real delivery slice.
card("23-A","统一任务契约对外模型","runtime",1,["29-A"],"UX-01 UX-03 F02",
     "evoagent/core/task.py evoagent/planning/intent.py evoagent/api/ evoagent/cli.py android-app/src/api.ts",
     "沿用Task权威，对齐自然语言目标/约束/产物/验收条件及REST、CLI、客户端边界",
     "三入口字段同源；明确兼容默认值与非法输入；general提示不能无条件屏蔽真实分类；明确约束不丢失")
card("23-B","聊天与工作模式及上下文转换","runtime",1,["23-A"],"UX-04 UX-05 UX-06 F01",
     "evoagent/core/controller.py evoagent/planning/ android-app/src/ android-app/App.tsx",
     "直接问答轻量完成；需要工具/产物时复用上下文进入正式任务，不复制执行内核",
     "F01-01/02与转工作场景通过；保留纠正和已确认约束；不无故重复已完成步骤")
card("23-D","用户可见状态与权威任务状态映射","runtime",1,["23-A"],"UX-08 F02 F03",
     "evoagent/api/ android-app/src/ android-app/App.tsx",
     "形成状态与等待原因的单一映射，协调两份产品规格的展示措辞并保留内部Task状态契约",
     "九态语义、暂停/待澄清/等待网络原因有映射；状态来自Task/事件；模型文字不能伪造完成或进度")
card("23-C","任务工作区与真实计划进度","android",1,["23-A","23-B","23-D"],"UX-07 UX-08 F02",
     "android-app/src/ android-app/App.tsx evoagent/api/",
     "展示目标、计划、节点依赖、真实阶段与用户待办，复用Task/Run/Node标识",
     "六种节点态可辨；阶段有权威记录；无依据不显示百分比；部分成果和待授权可见")
card("24-A","真实成果交付、验收与局部修改","assets",1,["23-C"],"UX-11 UX-12 UX-13 F02",
     "evoagent/ android-app/src/ android-app/App.tsx",
     "定义产物标识/版本/校验/可访问性，与Task.result和result_evidence复用；提供预览下载及局部修改路径",
     "F02-01/03有真实文件、独立验收、限制和摘要；无文件不显示下载；变更只重做受影响产物")
# Phase 2: user controls and existing runtime gaps.
card("23-E","用户控制、目标变更与安全恢复","android",2,["23-C","07-C"],"UX-09 UX-10 F03",
     "android-app/src/ android-app/App.tsx evoagent/api/ evoagent/core/controller.py",
     "接通暂停/取消/继续/变更与恢复用户路径，保留历史及未知外部动作",
     "F03-01—05逐项有执行与重启证据；授权不随目标扩大；未知副作用先对账，不盲目重放",
     "设备路径验收需要已授权真机；可先做后端/客户端自动化")
card("23-F","语音输入与文字任务共用入口","android",3,["23-A","23-B"],"UX-02",
     "android-app/src/ android-app/android/ android-app/App.tsx",
     "语音转写进入同一任务契约，声明识别/播报及离线范围",
     "转写完成不等于执行成功；转写错误可修正；高风险外部动作仍走动作授权",
     "真实语音组件、权限和设备可用；缺依赖明确显示不可用")
card("24-B","能力状态、权限与渐进授权","android",2,["23-A"],"UX-22 UX-23",
     "android-app/src/ android-app/App.tsx evoagent/api/",
     "复用注册表、依赖与权限服务展示能力状态、授权和撤销，避免配置存在即显示可用",
     "能力六档及具体原因可见；依赖外部服务如实展示；拒绝/撤销后受保护动作不能执行")
card("24-C","任务历史、项目空间与跨会话延续","android",3,["23-C","24-A"],"UX-14 UX-15 UX-16",
     "android-app/src/ android-app/App.tsx evoagent/api/ evoagent/memory/",
     "按项目归集任务与成果，恢复已确认目标、约束、产物与历史，不建立第二套Task状态",
     "跨会话延续引用原任务/项目；历史不被覆盖；隔离项目上下文，已完成步骤不重复执行")
card("24-D","用户记忆、隐私与纠正删除路径","models",3,["24-C","16-P"],"UX-17 UX-24 GLOBAL",
     "android-app/src/ android-app/App.tsx evoagent/memory/ evoagent/api/",
     "用户可看来源、纠错、删除与调整记忆/分析范围，消费现有命名空间和删除服务",
     "GLOBAL-02后续使用纠正事实；缓存和服务端范围一致；保留项/失败项明确，不宣称未验证的全域擦除")
card("24-E","成本与资源的用户可见控制","android",2,["23-C"],"UX-25 GLOBAL",
     "android-app/src/ android-app/App.tsx evoagent/api/",
     "展示预估/实际/未知成本与预算停止原因，复用现有账本而非另算余额",
     "GLOBAL-03预算耗尽停止新增调用且保留成果；unknown不能当0金额；调整额度不能绕过动作准入")
# Phase 3/4: proactive help, reusable knowledge, dynamic teams.
card("25-A","有来源的主动帮助与机会建议","models",3,["24-C"],"UX-18 UX-19 UX-20 F04",
     "evoagent/goals/ evoagent/memory/ android-app/src/ android-app/App.tsx",
     "复用OpportunityService/GoalGovernor，接授权工作范围、近期整理与建议交付",
     "F04-01/02有来源/事实推断未知区分；无高价值机会可不建议；建议含收益、权限和成本，不能自动外部执行")
card("25-B","主动强度、停止与资源非干扰","runtime",3,["25-A","15-D"],"UX-21 F04 G5",
     "evoagent/goals/ evoagent/core/budget.py android-app/src/ android-app/App.tsx",
     "四档主动程度、独立预算/队列、有限范围和无进展停止；核心任务优先",
     "F04-03/04在资源压力下实测；主动失败不修改核心状态；关闭不启动新非必要任务且保留安全提示",
     "设备或宿主资源测量环境；单纯Mock预算计数不算非干扰验收")
card("26-A","能力缺口证据与主动规划候选","runtime",4,["25-A","06-R"],"A-59",
     "evoagent/goals/ evoagent/evolution/",
     "从真实失败/用户目标与能力清单识别缺口，生成有范围、收益和成本的候选规划",
     "缺口非泛泛建议；证据可追溯、可去重、可人工确认；不绕过候选评测与发布")
card("26-B","动态团队与跨专家协作契约","runtime",4,["26-A","07-C"],"A-53 A-60",
     "evoagent/experts/ evoagent/planning/ evoagent/workflows/",
     "复用ExpertRegistry与DAG做团队角色、输入输出、冲突和失败恢复",
     "真实跨专家任务有契约与唯一状态写入者；冲突可解释；局部失败不越权重放外部动作")
card("27-A","故障经验沉淀与跨专家迁移验证","assets",4,["06-R","10-E"],"A-69 B-81 B-88",
     "evoagent/memory/ evoagent/knowledge/ evoagent/evolution/",
     "知识有来源/可信度/适用域/版本与撤销，注入前检查适用性并验证迁移效果",
     "不适用条目不注入；错误经验可撤销；跨专家前后对照证明收益，不能用命中率代替任务质量")
card("27-B","可组合能力与动态工作流资产","assets",4,["26-B","27-A"],"A-70 B-85",
     "evoagent/workflows/ evoagent/experts/ evoagent/assets/",
     "把验证过的跨专家流程沉淀成可组合、版本固定的能力资产",
     "依赖闭包与输入输出兼容；重用有真实执行证据；升级不改变已在运行的组合版本")
card("28-A","外部事实核验与可信交付","models",5,["24-A","16-P"],"A-61",
     "evoagent/verification/ evoagent/memory/ evoagent/api/",
     "对外部事实收集来源并交叉核验，区分矛盾、推断、未知和适用时间",
     "引用可访问且支持结论；来源冲突可见；无外部证据不得仅凭模型自述判可信",
     "合法可用的外部数据源/服务；P2后续，不阻塞本地首卡")
card("29-C","问题反馈与运行证据关联","integration",3,["23-C"],"UX-26",
     "evoagent/api/ evoagent/observability/ android-app/src/ android-app/App.tsx",
     "用户反馈绑定真实task/run/manifest与可授权的最小证据",
     "反馈不泄露凭据；来源与时间可定位；重复反馈可聚合，未验证原因不能当成根因")
card("29-D","改进效果与用户收益说明","integration",4,["29-C","10-E"],"UX-27",
     "evoagent/api/ evoagent/evolution/ android-app/src/ android-app/App.tsx",
     "把候选、评测与发布后效果翻译成可核验的用户变化说明",
     "说明区分候选/发布/观测结果；无真实对照不声称收益；退化与限制可见")
card("29-E","稳定版本、更新控制与回滚体验","android",3,["11-P","24-B"],"UX-28 GLOBAL",
     "android-app/src/ android-app/App.tsx evoagent/release/",
     "用户能理解稳定/实验版本、控制更新并查看真实回滚结果",
     "GLOBAL-01运行绑定不可无约束热切换；拒绝坏包可见；生产签名与回滚证据绑定同一版本",
     "生产信任材料与可连接设备")

# Existing parent gaps: distinct follow-up cards, no re-opening accepted slices.
old = [
("02-P","真实供应商副作用与进程故障恢复","runtime",2,["02-A"],"UX-10 F03","evoagent/tools/ evoagent/core/","真实外部适配器消费副作用预记账，验证崩溃和超时不重复执行","外部状态未知先对账；进程退出窗口、重试/补偿受限且有真实回执","真实供应商或受控外部测试服务"),
("03-P","生产信任根与元数据制品联验","assets",2,["03-S"],"G2 G6","evoagent/release/","生产公钥身份、元数据签名与实际制品字节一致消费","错密钥/篡改元数据/篡改制品均拒绝；密钥轮换可追溯，不分发签名私钥","生产或明确标注测试信任材料"),
("04-V","组合状态与生产身份恢复矩阵","runtime",2,["04-R","04-UI"],"F03 G3","evoagent/core/ evoagent/api/","扩展已验收四路径，验证组合任务/审批/节点状态与生产身份恢复","旧授权不跨身份复用；未知在途保留；覆盖检查点、重启与组合资产","真实部署身份与故障注入环境"),
("06-R","精确能力版本的运行时消费","runtime",1,["06-A"],"GLOBAL G6","evoagent/assets/ evoagent/core/controller.py evoagent/experts/","执行步骤消费不可变资产/依赖闭包，不仅存储和声明版本","实际工具/专家引用精确版本与digest；退役不可新绑定；旧run版本保持固定","本地制品及运行环境"),
("07-C","原子绑定、精确步骤授权与对账消费","runtime",1,["07-B","07-P"],"UX-09 UX-10 F03","evoagent/core/ evoagent/planning/ evoagent/workflows/","消除绑定崩溃窗口，将授权范围与步骤/版本/副作用对账原子消费","崩溃重启无半绑定；授权不扩大/重复消费；真实恢复先对账后决定","可重复故障注入"),
("08-W","工作流与执行模式的主链路由","models",2,["23-A","08-R"],"UX-03 UX-06 B-75","evoagent/routing/ evoagent/core/controller.py evoagent/workflows/","路由进入工作流/执行模式实际选择并保留拒绝/降级证据","入口与评测一致；不可用默认不旁路使用；决策与真实执行归因一致","本地路由夹具可开发；真实链另采证"),
("08-P","生产评测集、价格档案与费用对账","models",3,["08-M"],"UX-25 G4","evoagent/routing/ evoagent/models/ evoagent/core/budget.py","建立代表性任务集、真实供应商版本价格与未知费用后续对账","价格有来源/有效期；未知只计数不造金额；计量与账单差异可解释","供应商价格/账单与脱敏真实任务集"),
("09-H","真实候选宿主、持久队列与进程隔离","assets",2,["09-F"],"B-78 G2 G3","evoagent/evolution/ evoagent/sandbox/","隔离宿主执行真实候选资产，持久问题队列并验证多进程恢复","超时强杀；网络/凭据边界；崩溃后队列和候选身份可恢复","受控宿主与真实候选制品"),
("10-E","真实候选收益与依赖回归证据","assets",3,["09-H","10-H"],"B-76 B-77 B-78 B-79 B-96 B-97 G1","evoagent/evaluation/ evoagent/evolution/","消费真实宿主/制品版本，建立依赖回归范围和发布前后收益链","实际覆盖有范围/遗漏说明；对照可复现；退化阻止发布或触发既定回滚","代表性基准/对照环境"),
("11-P","生产签名与端侧安装退役回执","android",2,["03-P","11-W"],"G0 G6","android-app/src/ android-app/android/ evoagent/release/","联验生产信任、公钥验签、安装/激活/退役回执与灰度回滚","回执绑定真实设备/制品；坏包拒绝与回滚旧能力实测；断联不冒充已安装","可连接设备与生产或标注测试的签名材料"),
("12-E","自然语言手机App真实任务主链","android",2,["23-C","07-C","06-R"],"F02 F03 G0 G1","android-app/src/ android-app/android/ android-app/App.tsx evoagent/api/","从自然语言到真实设备动作、授权、回执、成果的完整链路","明确授权后真机执行；task/run/effect一致；夹具transport不能算真机动作","可连接且授权的Android设备"),
("12-V","通知、离线与系统重启真机矩阵","android",2,["12-A"],"F03 G0 G3","android-app/src/ android-app/android/","验证271378e后通知新APK以及离线/杀进程/物理重启组合行为","成功/失败/取消通知真实；渠道关闭不误计投递；系统重启回执不重复副作用","可连接设备，记录APK/JS精确版本"),
("13-P","生产长期目标推进与周期守护","runtime",3,["13-A","14-A"],"F04 B-90 B-91","evoagent/goals/","消费现有Goal/Opportunity服务做有授权周期推进与负载守护","停止/预算/授权持续有效；无进展停止；真实负载不饿死主任务","运行时调度与负载环境"),
("15-P","Provider/Expert预算原子化与旧Goal迁移","runtime",2,["15-R"],"GLOBAL G4","evoagent/core/budget.py evoagent/goals/ evoagent/models/","补provider/expert预留原子性与旧Goal账户迁移，复用总账","并发不超留；迁移可回退且余额一致；供应商未知金额不得假定零","可重复并发和迁移夹具"),
("15-D","设备资源准入与核心任务优先","runtime",3,["15-P"],"F04 G4 G5","evoagent/core/ evoagent/goals/ android-app/src/","CPU/内存/模型资源准入、低优先暂停及恢复，向主动帮助提供统一能力","资源压力下主任务满足明确阈值；主动分析让出资源；停止不损坏核心状态","真实设备或受控宿主资源测量"),
("16-P","业务仓储与端侧隐私凭证治理","integration",3,["16-X"],"UX-17 UX-24 G2","evoagent/api/ evoagent/memory/ evoagent/observability/ android-app/src/","覆盖尚未接入的业务仓储、缓存和端侧EvidenceEvent，明确IAM及删除边界","受信身份隔离；最小证据与脱敏；实际删除/保留/失败可对账","生产身份/存储范围及端侧测试环境"),
("17-P","空机部署、组合恢复与RTO/RPO","integration",4,["17-A","11-P"],"G0 G3 G7","evoagent/recovery/ evoagent/deployment/","空环境部署并恢复组合资产、身份、账本与备份，测量RTO/RPO","固定备份/版本可复现；不兼容拒绝；失败回退与真实时间/损失窗口记录","干净部署环境与真实备份"),
("18-G","App与编程专家真实消费能力包","runtime",4,["18-F","06-R","24-A"],"F02 A-60","evoagent/experts/ evoagent/tools/ android-app/src/","在真实项目通过编程专家/用户任务消费现有包，而非仅调用工具单测","跨文件修改/验收/回滚闭环；包生命周期与精确版本记录；未实现LSP不宣称支持","受控真实项目"),
("18-L","跨文件语义与LSP能力扩展","runtime",5,["18-G"],"A-60 A-70","evoagent/tools/ evoagent/experts/","按真实编程任务引入语言服务与跨文件语义验证","固定语言服务版本；诊断/修改前后对照；无法确定的重构请求澄清","语言服务依赖与真实项目"),
("19-A","真实音视频供应商漫剧纵切片","assets",5,["24-A","15-P"],"F02","evoagent/experts/ evoagent/tools/","按原ALN19关闭标准接真实音视频制品和计划/评测/费用","真实生成可播放产物；费用/版本/授权可追溯；失败有界恢复","真实服务配置与预算"),
("20-P","固定外部Runtime真实PoC","runtime",4,["20-A"],"B-75 G2 G3","evoagent/runtime/ evoagent/adapters/","固定外部Harness/Runtime版本真实执行并验证主链采用边界","兼容矩阵与真实版本一致；不绕过授权/账本/停止；失败隔离","固定外部运行时及可用安装环境"),
("21-A","Windows或平板扩展验证","android",5,["17-P","20-P","12-E"],"G0 G3","android-app/ evoagent/","Android稳定后按原ALN21范围验证后续终端部署与状态恢复","固定端版本/身份/回执；终端差异明确；不复制第二套执行权威","目标终端"),
("22-A","原全量范围与新增产品证据汇总","integration",5,["29-B","29-E","17-P","19-A","21-A"],"G0 G1 G2 G3 G4 G5 G6 G7","docs/协作开发/验收/ tools/","汇总原23包和新增产品包证据，各自关闭标准分开验证","所有未满足项显式保留；精确版本证据齐备；开发完成不等于发布批准","原包依赖和真实外部环境逐项满足"),
]
for values in old:
    card(*values)

ids = [s["id"] for s in specs]
assert len(ids) == len(set(ids))
existing = {d["id"] for d in board["dispatches"]}
assert not set(ids) & existing
known = existing | set(ids)
assert all(set(s["depends_on"]) <= known for s in specs)
ux_map = {f"UX-{n:02}": [s["id"] for s in specs if f"UX-{n:02}" in s["requirement_refs"]] for n in range(1,29)}
assert all(ux_map.values())
case_map = {
    "UX-F01-01":["ALN-23-B"],"UX-F01-02":["ALN-23-B"],
    "UX-F02-01":["ALN-23-C","ALN-24-A","ALN-12-E"],"UX-F02-02":["ALN-02-P","ALN-23-E"],
    "UX-F02-03":["ALN-24-A"],
    **{f"UX-F03-{n:02}":["ALN-23-E","ALN-07-C","ALN-12-V"] for n in range(1,6)},
    "UX-F04-01":["ALN-25-A"],"UX-F04-02":["ALN-25-A"],
    "UX-F04-03":["ALN-25-B","ALN-15-D"],"UX-F04-04":["ALN-25-B"],
    "UX-GLOBAL-01":["ALN-06-R","ALN-29-E"],"UX-GLOBAL-02":["ALN-24-D"],
    "UX-GLOBAL-03":["ALN-24-E","ALN-15-P"],
}
plan = dict(id="PRODUCT-2026-10-25", authorized_by="user_request",
    request="先根据现状和设计以及预期对需求进行拆分一起一起认领开发",
    source_commit=head, sources=source, new_workpackages=packages, tasks=specs,
    ux_coverage=ux_map, acceptance_coverage=case_map,
    technical_coverage=".planning/2026-10-25-readme-design-review/design-mapping.json",
    dependency_note="包依赖约束整包关闭；卡片depends_on约束实际接口接线，允许使用已交付待审接口准备实现，不能跳过验收",
    phases={"0":"验收公共尺子","1":"任务与真实成果纵切片","2":"控制恢复与已有运行时缺口",
            "3":"项目信任、有限主动帮助与资源实测","4":"迁移/团队/外部Runtime与生产恢复","5":"P2场景与全量收口"})
(root / plan_relative).write_text(json.dumps(plan, ensure_ascii=False, indent=2)+"\n", encoding="utf-8", newline="\n")
board["workpackages"] += packages
for spec in specs:
    identity = spec["id"]
    own = identity in {"ALN-29-A","ALN-23-A"}
    shared = [entry["path"] for entry in board["shared_files"]
              if any(entry["path"] == p or entry["path"].startswith(p.rstrip("/")+"/") for p in spec["write_paths"])]
    filename = re.sub(r'[<>:"/\\|?*]', "-", spec["title"])
    relative = f"docs/协作开发/任务卡/{identity[4:]}-{filename}.md"
    dispatch = dict(id=identity, workpackage=spec["workpackage"], title=spec["title"], stream=spec["stream"],
        owner="codex-runtime" if own else None, reviewer=None, branch=branch if own else None,
        base_commit=head if own else None, status="in_progress" if identity=="ALN-29-A" else "claimed" if own else "unclaimed",
        blocker=None, evidence=[], card=relative, shared_files=shared,
        requirement_batch=plan["id"], depends_on=spec["depends_on"], phase=spec["phase"],
        requirement_refs=spec["requirement_refs"], next_step="按卡实施并提交证据；未经复核不记accepted",
        working_basis="沿用5e2b7e3现有已交付实现；实际接手锚点并非已发布共同基线",
        prerequisites=spec["prerequisites"])
    board["dispatches"].append(dispatch)
    lines=[f"# {identity}｜{spec['title']}","",f"来源批次：{plan['id']}；阶段{spec['phase']}；父包{spec['workpackage']}。",
        f"需求：{' / '.join(spec['requirement_refs'])}；来源：[分期需求清单](../需求拆分与分期实施_2026-10-25.json)。",
        "","## 目标","",spec["goal"],"",
        "复用已有实现，先读当前接口与交付记录；不重新实现已接受切片。",
        "","## 依赖与开工条件","",f"- 卡片依赖：{', '.join(spec['depends_on']) or '无'}",
        f"- 环境：{spec['prerequisites']}",
        "- 包依赖约束整包关闭；待审接口可用于独立实现，但不能替代真实集成验收。",
        "","## 写入边界","",*["- "+p for p in spec["write_paths"]],
        "","共享文件按workboard.shared_files登记；多人认领时先声明精确修改点，同文件串行合入。新增迁移需集成排号。",
        "","## 验收","",spec["acceptance"],"",
        "- 记录正向、失败与恢复/拒绝路径；Mock/夹具明确标注，不能冒充真机或生产实测。",
        "- 绑定精确源码commit、命令、结果、日志及必要环境；无证据保持未测试。",
        "","## 交付","",
        "- 在交付目录提供本卡报告、兼容/迁移影响与保留缺口，填写台账evidence和delivered_commit。",
        "- 开发完成转review；审阅人未定保留null，不自批为独立验收；发布决定另行处理。",
        "- owner/branch/base_commit以workboard为准；未认领卡不预填他人身份。",""]
    (root / relative).write_text("\n".join(lines), encoding="utf-8", newline="\n")
board.setdefault("requirement_batches", []).append(dict(id=plan["id"], plan=plan_relative))
board["updated_at"]="2026-10-25"
board["execution_queue"]=dict(mode="当前项目单会话串行；其他开发者按待认领卡独立接手",owner="codex-runtime",wip_limit=1,
    active_task="ALN-29-A",next_task="ALN-23-A",items=[
        dict(id="ALN-29-A",state="in_progress",condition="先交付17条用例的断言、证据格式及未测试语义"),
        dict(id="ALN-23-A",state="claimed",condition="29-A交付后按Task权威开发统一契约，实际开工更新base_commit")])
board["execution_note"]="按新增分期计划逐卡开发，WIP=1。已认领29-A和23-A；其余新增卡owner=null。包依赖约束关闭，卡依赖约束接线；待审接口可用于实现，不代表已接受或发布。基线仍candidate，published_commit为空。"
board_path.write_text(json.dumps(board,ensure_ascii=False,indent=2)+"\n",encoding="utf-8",newline="\n")
print(json.dumps(dict(workpackages=len(board["workpackages"]),new_cards=len(specs),dispatches=len(board["dispatches"]),
                     claimed=["ALN-29-A","ALN-23-A"],active="ALN-29-A"),ensure_ascii=False))
