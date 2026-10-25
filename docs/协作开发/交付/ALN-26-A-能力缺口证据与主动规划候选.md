# ALN-26-A 能力缺口证据与主动规划候选

任务：ALN-26-A / 能力缺口证据与主动规划候选
负责人：dsh-lead（本会话认领，先登记 claim 再实现）
审阅人：待定（未分配独立审阅人，不自批为独立验收）
基线 commit：`95f3e38aaeae06434edbe6b8d8d832e02640cc61`（认领时 `feature/user-direction` HEAD，不是已发布共同基线）
分支与最终 commit：`feature/user-direction` / `662c25898b7f3e82ea898cfd69439a43cb8702bb`（tree `2748f1b86a67838407e8455e932952787e6bf734`）
认领状态：claimed → **review**

## 触发场景与行为变化

- 输入/触发：系统要从**真实信号**（失败任务、兜底路由、未满足用户目标）识别能力缺口，并给出**有范围、收益与成本**的改进候选；同时不能变成「泛泛建议」，不能绕过候选评测与发布。
- 修改前：
  - 25-A 已有有来源的主动帮助与机会建议（`ProactiveAssistService`）、06-R 精确能力版本运行时消费、EvolutionController 候选流程；但**没有把「能力缺口证据」聚合成可追溯缺口、再转成候选计划**的层：
    - 缺口来源分散在失败任务与 `ExpertRegistry.route_with_match` 的 fallback 信号里，未聚合去重；
    - 没有阈值门控（单条弱证据会被当成缺口）；
    - 没有「证据引用仍存在」的可追溯校验；
    - 候选与证据没有结构化关联。
- 修改后（新增 `evoagent/goals/gap_planner.py`，**未改** 既有 goals/experts/evolution 模块）：
  - `CapabilityGapPlanner.collect_evidence()`：三类可追溯证据——失败/取消任务（要求 failure_reason）、能力清单兜底路由（`route_with_match` 返回 fallback=False/未命中）、未满足目标（非终态目标 + 其下失败任务）；
  - `analyze()`：按能力签名聚合、按 `ref` 去重；`min_evidence` 阈值 + 「至少一条带失败原因的任务证据，或足够兜底路由证据」门控；**不达标进 `insufficient` 并写明原因，不生成候选**（fail-closed）；
  - `traceability()`：规划前逐条核对 `task:*` / `goal:*` 引用仍存在；失配即拒绝规划；
  - `plan_candidates()`：对可晋升缺口生成 `benefit`（受影响任务数/频率/严重度）与 `cost`（按证据数估算的单位预算，非真实账单），并**只经既有 `EvolutionController.propose_candidate`** 登记（`requires_evaluation=True`、`policy` 明确不绕过评测/门禁/发布；规划器**不做部署**）。

## 范围

- 独占模块：`evoagent/goals/gap_planner.py`（新增）。
- 共享文件补丁：**无**（未改 goals/experts/evolution/api 既有文件）。
- 新增/修改契约：缺口证据、缺口聚合、可追溯校验、候选计划四类结构；无 schema 变更、无迁移。
- 兼容：纯新增模块，既有目标/专家/演化行为不变。

## 验证证据

- 正向：
  - 两条同签名失败任务 → 1 个缺口、2 条可追溯证据、distinct_tasks=2、promotable；
  - 兜底路由证据（未知意图/领域）2 条 → 缺口可晋升；
  - 可晋升缺口 → `plan_candidates` 生成候选并**经 EvolutionController 登记**（`candidate_id` 可查、`requires_evaluation=True`），规划器未触发部署（发布指针不变）；
  - 未满足目标：非终态目标下的失败任务 → `unmet_goal` 证据与 `goal:*` 引用进入缺口（测试覆盖）。
- 拒绝/不足路径：
  - 单条失败且无失败原因 → `insufficient`（原因可见），0 缺口；
  - `min_evidence=3` 时 2 条证据 → 不足；`min_evidence=0` → `GapPlannerError`；
  - 引用 `task:vanished` / `goal:goal_nope` / 非 task/goal 前缀 → 可追溯校验失败，规划被拒；
  - 证据不足的缺口永不进入 `planned`。
- 命令、结果、日志与对应源码 commit：
  - 提交 `662c25898b7f3e82ea898cfd69439a43cb8702bb`；
  - 本卡专项：`python -m pytest tests/test_gap_planner.py -q` → **9 passed**；
  - 目标/演化相关域回归（test_goal* + test_evolution* + 本卡）→ **165 passed（31.92s）**；
  - 证据生成：`.planning/2026-10-25-aln26a/gap-planning-evidence.json`，`all_checks_pass=true`（8 项检查）；
  - 全量后端：`python -m pytest tests/ -q` → **2098 passed / 1 failed / 1 skipped（408.58s）**，日志 `.planning/2026-10-25-aln26a/backend-tests.txt`。
  - 唯一失败 `tests/test_developed_review_3.py::test_empty_file_change_is_real_artifact` **与本卡无关**：该文件属他人 ALN-24-D/评审修复线（a1b3a31），单独运行该文件 **26 passed**，为全量顺序下的测试隔离问题；本卡只新增 `goals/gap_planner.py`，不触及制品归档/快照逻辑。
- **真实数据证据（重点）**：
  - 对仓库内 `.evoagent/evoagent.db`（真实历史任务库，**只读复制**后分析，未改原库）跑分析：94 SUCCEEDED / 2 FAILED；
  - 2 条失败各 1 条证据 → **两个缺口均为 insufficient、0 候选**（真实数据上 fail-closed 生效，不把单次失败当缺口）；
  - 失败任务 id 与 `task:*` 引用逐条可回查（`real_task_refs_traceable=true`）。
- Mock / 夹具 / 真实边界：
  - 真实数据部分：仓库历史 DB 的真实失败记录（只用于分析，未写入原库）；
  - 晋升路径用**明确标注的 fixture** 演示（2 条同签名失败 → 缺口 → 候选登记），不是生产数据；
  - 兜底路由信号基于 `builtin_experts()` 的能力声明，非线上路由流量；
  - 候选的**评测与发布未在本卡执行**（本卡只到 propose 登记）；成本为估算单位预算，不是账单。

## 回滚与未完成事项

- 回滚触发、操作及验证：`git revert 662c25898b7f3e82ea898cfd69439a43cb8702bb`（删新增模块与测试；无 schema/迁移）。回滚后既有目标/演化行为不变。
- 保留的限制、后续 ALN 和依赖：
  - 缺口信号当前来自本地历史任务与兜底路由；未接线上遥测/用户反馈流；
  - 真实库失败样本过少（2 条）→ 真实数据未触发候选晋升，仅验证 fail-closed；
  - 未接 API/UI（用户无法看到缺口证据与候选计划）；
  - 成本/收益为估算，未接真实预算台账；
  - 候选仍由既有评测/发布流程处理，本卡不改变门禁。
- 影响的主题/实体/ADR/契约/系统验收条目：新增缺口证据/候选计划契约；既有目标/演化/发布契约不变；A-59 的「从真实失败与用户目标识别缺口→候选计划」服务端部分交付。

## 审阅结论：待审

## 整包结论：仍实现中（ALN-26：线上遥测接入、API/UI 展示与真实预算挂账仍待；本卡交付缺口证据与候选计划层）


## 第四批审查修正（2026-10-25）

修复提交6ae4b30525e30e6f7278953a0c5e8772e4727dcb。规划重新核对权威分析；按独立任务事件而非目标投影计阈值；空/过期/改写证据拒绝；无登记服务不假提案；同问题与证据复用候选。目标来源与失败根因仍是待验证信号，不自动发布。

新增修复待固定提交独立复核；旧证据保留为历史，详见[第四批审查](../验收/2026-10-25-第四批主动规划与组合资产审查.md)。
