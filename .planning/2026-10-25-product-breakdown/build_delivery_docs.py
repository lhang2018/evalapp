"""Build the acceptance matrix and human-readable backlog from their source data."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root/"tools"))
from product_acceptance import ASSERTIONS, SOURCE, source_rows

steps = {
    "UX-F01-01":["输入可直接回答的概念问题，不要求执行外部操作","采集回答及实际新建计划/工作流记录，确认未启动不必要复杂任务"],
    "UX-F01-02":["在同一会话提出依赖前文的追问，并保留一条明确约束","核对后续回答使用相关上下文且未丢弃该约束"],
    "UX-F02-01":["提交带格式与质量要求的多步骤报告任务","实际生成文件并从交付入口打开，调用逻辑独立验收器检查必选条件"],
    "UX-F02-02":["在多步骤任务中注入一次可定位的工具失败，设置有限重试预算","采集失败识别、实际重试次数、上限和终止/恢复记录"],
    "UX-F02-03":["生成不满足至少一条强制验收条件的产物","确认验收失败项可见，任务未显示全部完成"],
    "UX-F03-01":["在执行中追加会影响产物的新约束","采集约束保留、依赖影响分析和计划修订记录"],
    "UX-F03-02":["在已有完成步骤的任务中改变目标","核对历史保留、受影响步骤重评估和未受影响结果复用"],
    "UX-F03-03":["任务有在途操作时请求取消","核对不再新增非必要动作，并完成在途操作查询/核对或明确未知处置"],
    "UX-F03-04":["在检查点后实际退出/重启目标运行环境，再发起恢复","核对权威状态、外部回执与盲目重放次数，记录实际重启方式"],
    "UX-F03-05":["制造已提交外部动作但本地回执未知的状态","采集先对账、后决定继续的事件顺序和实际外部执行次数"],
    "UX-F04-01":["选择明确授权的项目/会话范围，触发近期工作整理","逐项核对摘要事实来源，区分事实、推断和未知"],
    "UX-F04-02":["提供没有高价值机会的近期工作样本","确认机会评分不达门槛时不会强行输出建议"],
    "UX-F04-03":["并行运行核心任务与主动分析，施加可测量资源压力","按预先声明阈值采样，核对主动让出资源且未修改核心状态"],
    "UX-F04-04":["关闭主动帮助后再次触发调度，同时保留核心任务和安全提示场景","核对无新增非必要主动任务，核心执行和必要安全提示可用"],
    "UX-GLOBAL-01":["运行任务绑定版本A，期间尝试后台激活版本B","核对该运行前后Manifest绑定保持A，未发生无约束热切换"],
    "UX-GLOBAL-02":["让用户纠正一条已进入摘要的事实，再触发后续处理","核对使用纠正后的事实，没有复用已知过时结论"],
    "UX-GLOBAL-03":["保留已完成产物后耗尽任务预算","核对不再新增调用，已生成可用成果仍可访问"],
}
rows = source_rows(root)
matrix = dict(schema_version=1, scope="17 original first-release scenarios; recorded observations are not independent acceptance",
    source=dict(path=SOURCE,sha256_lf=hashlib.sha256((root/SOURCE).read_bytes().replace(b"\r\n",b"\n")).hexdigest()),
    default_status="not_run",
    cases=[dict(id=i,scenario=rows[i][0],source_condition=rows[i][1],initial_status="not_run",steps=steps[i],
                assertions=[list(p) for p in ASSERTIONS[i]]) for i in sorted(rows)])
(root/"docs/协作开发/验收/首发验收矩阵.json").write_text(json.dumps(matrix,ensure_ascii=False,indent=2)+"\n",encoding="utf-8",newline="\n")
plan_path=root/"docs/协作开发/需求拆分与分期实施_2026-10-25.json"
plan=json.loads(plan_path.read_text(encoding="utf-8"))
board_path=root/"docs/协作开发/workboard.json"
board=json.loads(board_path.read_text(encoding="utf-8"))
# Exact Git identities can be expanded without changing another developer's claim.
branch=subprocess.check_output(["git","branch","--show-current"],cwd=root,text=True).strip()
for d in board["dispatches"]:
    if d.get("requirement_batch")==plan["id"] and d["owner"] and d.get("base_commit"):
        original=d["base_commit"]
        exact=subprocess.check_output(["git","rev-parse","--verify",original+"^{commit}"],cwd=root,text=True).strip()
        if exact!=original:
            d["base_commit"]=exact
            d["claim_metadata_calibration"]=dict(original_base_commit=original,reason="展开已存在Git对象；保留原认领者、状态与接手时点")
        if not d.get("branch"):
            d["branch"]=branch
            d["branch_scope"]="observed shared checkout; not a dedicated developer branch"
board_path.write_text(json.dumps(board,ensure_ascii=False,indent=2)+"\n",encoding="utf-8",newline="\n")

by_id={d["id"]:d for d in board["dispatches"]}
doc=["# 需求拆分与分期实施（2026-10-25）","",
     "依据用户要求，将现状、设计和预期拆成正式工作包及可领取任务卡。机器来源为[分期清单](需求拆分与分期实施_2026-10-25.json)，认领状态以[workboard](workboard.json)和[认领表](开发任务认领表.md)为准。",
     "","## 范围与状态","",
     "- 保留原ALN-00—22的标题、依赖和历史验收；登记ALN-23—29七个新包，合计30包。",
     "- 新增46张卡：23张承接原包保留缺口，23张落实新增产品需求；加上原41张，共87张。",
     "- 28项UX与17条用例全部映射到实施卡；后续技术专项沿用原包，不能把主题已映射当作功能已完成。",
     "- codex-runtime认领29-A（验收骨架）和下一张23-A（统一任务契约）；每位开发者同时开发一张。",
     "- 多位开发者已开始认领，保留其认领和代码；当前owner=null的卡可按边界领取，实际进度以实时台账为准。",
     "- 00-B仍review，532a364仍candidate且未发布。既有接口可用于独立开发，不能用开发完成替代发布决定。",
     "","## 开发方式与依赖","",
     "包依赖约束整包关闭；卡片depends_on约束实际接口接线。可以先写独立模块、契约和明确标注的夹具，不必等所有父包关闭；真实验收仍要求对应依赖可用。",
     "阶段表示建议交付波次，不要求前一阶段所有外部条件齐备后才允许后续独立工作。共享controller/Task/鉴权/API装配/App.tsx/API模型由对应角色串行合入；同文件不得并行覆盖。",
     "认领先更新owner、当前branch、精确base_commit与状态，单卡提交。未分配审阅者留null，交付后转review；Mock、断言工具自测和未执行场景均不得标成真实功能通过。",
     "","## 新包关闭标准","",
     "| 包 | 优先级 | 范围 | 关闭标准 |","|---|---|---|---|"]
for p in plan["new_workpackages"]:
    doc.append(f"| {p['id']} | {p['priority']} | {p['title']} | {p['closure_criteria']} |")
for phase,label in plan["phases"].items():
    doc += ["",f"## 阶段{phase}：{label}","","| 卡 | 任务 | 实施依赖 | 认领 | 环境前提 |","|---|---|---|---|---|"]
    for t in plan["tasks"]:
        if t["phase"]!=int(phase):continue
        d=by_id[t["id"]]
        doc.append(f"| [{t['id']}]({d['card'].split('docs/协作开发/')[1]}) | {t['title']} | {', '.join(t['depends_on']) or '无'} | {d['owner'] or '待认领'} | {t['prerequisites']} |")
doc += ["","## 覆盖与优先顺序","",
        "codex首批顺序：29-A → 23-A → 23-B/23-D → 23-C → 24-A。运行线可先做06-R/07-C，Android能力线24-B、预算线15-P可基于现有接口独立开发；各线接入App.tsx/API/运行共享文件时排队。",
        "之后完成23-E/12-E/12-V的用户控制与真机闭环，再补项目/记忆/主动帮助。P2语义工具、漫剧、多端与全量收口后置；不得为给旧包清零而跳过真实环境。",
        "新29包的关闭依赖采用00/10/11/16/23/24/25，不再把原22全量收口当首发所有子卡的前置，避免将后续多端/漫剧阻塞到验收骨架。",
        "","| 需求 | 承接卡 |","|---|---|"]
for requirement,owners in plan["ux_coverage"].items():
    doc.append(f"| {requirement} | {', '.join(owners)} |")
doc += ["","17用例逐项实施归属见JSON acceptance_coverage；原23包的其他保留范围仍以原实施计划和交付证据为准。新增卡只承接明确切片，不自动关闭父包。",""]
(root/"docs/协作开发/需求拆分与分期实施_2026-10-25.md").write_text("\n".join(doc),encoding="utf-8",newline="\n")
print(f"matrix={len(rows)}; backlog={len(plan['tasks'])}; UX={len(plan['ux_coverage'])}")
