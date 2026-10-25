# ALN-29-D 改进效果与用户收益说明

任务：ALN-29-D / 改进效果与用户收益说明
负责人：dsh-lead（本会话认领）
审阅人：待定（未分配独立审阅人，不自批为独立验收）
基线 commit：`f25092ca8f849e1e853e9915f7fd3d43550f52e3`（认领时的 `feature/user-direction` HEAD，不是已发布共同基线）
分支与最终 commit：`feature/user-direction` / `b265fd12aec5df9d1eb5a5cdaadf05a505920de7`（tree `194fd6b68fdf2ac0cdbe6d7c0b592c7f257052e6`）
认领状态：claimed → **review**

## 触发场景与行为变化

- 输入/触发：需要向用户说明「这次改进到底带来了什么变化」——候选/评测/发布/观测四类证据混在一起时，极易把「基准上更好」说成「线上更好」。
- 修改前：
  - 29-C 已交付用户反馈与运行证据关联，10-E 已交付真实宿主对照基准与收益/回归证据；但**没有把这两类证据翻译成用户可核验说明**的层：候选生命周期里的 `benefit_result`/`quality_result`/`monitor_result` 都是内部结构，缺阶段区分与「不可声称」约束。
- 修改后（新增 `evoagent/evolution/benefit_statement.py` + 注册表端点；`App.tsx`/客户端**未改**）：
  - `build_user_benefit_statement(candidate, benchmark_report=..., release=..., monitor=...)` 输出四阶段分开的说明：
    - `candidate`：候选已提出（目标组件/问题/状态）；
    - `pre_release`：发布前评测/基准对照（含报告 id、verdict、metrics、可复现性、十维向量）；
    - `release`：发布版本/范围（`deployed_release_id` 或显式 release）；
    - `observation`：发布后监测窗口（decision/metrics/window/sample_size）。
  - **fail-closed 硬规则**：
    1. 没有对照评测证据 → 不产生任何正向收益表述（`cannot_claim` 写明「没有对照评测证据」）；
    2. 基准 `reproducible=False` → 不产生 benchmark 收益表述；`regressed` → 进 `degradations` 且明确「不能声称改进」；
    3. 基准 `improved` 的表述强制限定「基准范围，不等于线上收益」；缺发布后监测 → `cannot_claim` 写明「不能声称线上收益」，`limitations` 记「发布后效果待观测」；
    4. 监测 `rollback / rollback_recommended` → `degradations.post_release_regression` + 禁止线上收益表述；监测 warnings 逐条进 `limitations`；收益向量里 `degraded=True` 的维度进 `degradations`。
  - 每条 `claims` 携带 `basis`（candidate/evaluation/benchmark/release/monitor）与 `evidence_refs`（评测报告 id / 证据摘要 / 监测 id），无证据的表述只能进 `cannot_claim`。
  - `scope` 显式标注 `candidate-only / evaluation-only / benchmark-only / release-monitored`，并给出对应的一句话摘要（例如「基准结论不等于线上收益」）。
  - 新增 `GET /evolution/candidates/{candidate_id}/benefit-statement`（注册表路由，`server.py` 仅加一行 import）；未知候选 404。

## 范围

- 独占模块：`evoagent/evolution/benefit_statement.py`、`evoagent/api/benefit_api.py`（新增）。
- 共享文件补丁（精确修改点）：`evoagent/api/server.py` —— 仅新增一行 `from . import benefit_api`；未改其 if 链与既有逻辑。
- 未改：`App.tsx` / `android-app/src/`（端侧展示待 UI 卡接线）、`evolution/{benefit,monitor,controller}.py`、`evaluation/host_bench.py`（只消费其报告结构，不反向修改）。
- 新增/修改契约：收益说明结构（`stages/claims/cannot_claim/degradations/limitations/scope/summary`）与 1 个 HTTP 端点；无数据库 schema 变更、无迁移。
- 兼容：纯新增模块与一个端点，既有候选/评测/监测 API 行为不变。

## 验证证据

- 正常路径：
  - 可复现基准 `improved`：`scope=benchmark-only`，benchmark claim 可核验且带 `evidence_refs`（评测报告 id/证据摘要），摘要明确「不等于线上收益」；
  - 发布后监测健康（`decision=keep`）：`scope=release-monitored`，monitor claim 可核验，`warnings` 进 `limitations`；
  - 发布阶段与观测阶段分离：仅有发布（`deployed_release_id`）时 `scope` 仍为 `candidate-only`，并写「已发布不等于线上有效」。
- 拒绝/退化/无证据：
  - 只有候选：`scope=candidate-only`，`claims` 只含候选提案（`verifiable=False`），`cannot_claim` 同时写「没有对照评测证据」「缺少发布后监测证据」；
  - 基准 `regressed`：`degradations` 含 `benchmark_regression`，无正向 benchmark 表述，`cannot_claim` 写「不能声称改进」；
  - 基准不可复现：不产生 benchmark 表述（fail-closed）；
  - 监测回滚：`degradations` 含 `post_release_regression`，禁止线上收益表述；
  - 十维向量某维度 `degraded=True`：`degradations` 含 `dimension_degraded`。
- HTTP：真实 `EvolutionController.propose_candidate` 的候选 → 200 且 `scope=candidate-only`；未知候选 → 404。
- 命令、结果、日志与对应源码 commit：
  - 提交 `b265fd12aec5df9d1eb5a5cdaadf05a505920de7`；
  - 本卡专项：`python -m pytest tests/test_benefit_statement.py -q` → **9 passed**；
  - 演化/接口相关域回归（evolution/benefit/api/signals/routing_eval/host_benchmark + 本卡）→ **102 passed（15.08s）**；
  - 全量后端：`python -m pytest tests/ -q` → **1983 passed / 1 failed / 1 skipped（508.57s）**，日志 `.planning/2026-10-25-aln29d/backend-tests.txt`。
  - 唯一失败 `tests/test_peer_action_binding_review.py::test_real_app_wires_bindings_and_persists_them_for_reconciliation` 与本卡无关：该测试属 ALN-07-C 系，工作区存在他人**未提交**的 `evoagent/planning/binding.py` 改动导致绑定证据未被持久化；本卡未改绑定/规划/ReAct 任何文件。
- Mock / 本地组件 / 真机 / 真实供应商的验证边界：
  - 本轮为本地后端 + 真实 HTTP 进程；评测/监测数据为构造证据与真实候选对象，不是生产流量；
  - **端侧展示未接**：App.tsx/Android 未改，用户尚不能在手机端看到该说明（属 UI/24-C 接线）；
  - 仅中文文案；未做 i18n；
  - 基准报告由 ALN-10-E 产出，本卡只消费；发布后监测由 monitor 线产出，缺数据时按 fail-closed 处理。

## 回滚与未完成事项

- 回滚触发、操作及验证：`git revert b265fd12aec5df9d1eb5a5cdaadf05a505920de7`（删两个新模块与测试、撤掉 server.py 一行 import；无 schema/迁移）。回滚后既有收益/监测 API 不变。
- 保留的限制、后续 ALN 和依赖：
  - 端侧界面（手机端展示改进说明）未接，属 24-C/UI 后续；
  - 说明只消费候选已持久化的结果；把 10-E 基准报告自动关联到具体候选（evidence 绑定）尚需评测线接线；
  - 未接发布后监测自动采集（monitor 线负责），本卡只保证「有证据才说、没证据不说」；
  - 文案未做用户分层/多语言。
- 影响的主题/实体/ADR/契约/系统验收条目：新增收益说明契约与 1 个端点；既有候选/评测/监测语义不变；UX-27 服务端部分交付。

## 审阅结论：待审

## 整包结论：仍实现中（ALN-29：首发范围证据与端侧展示仍待；本卡交付可核验的用户收益说明层）
