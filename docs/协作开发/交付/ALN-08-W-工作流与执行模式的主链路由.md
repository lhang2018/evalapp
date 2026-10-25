# ALN-08-W 工作流与执行模式的主链路由

任务：ALN-08-W / 工作流与执行模式的主链路由
负责人：dsh-lead（本会话认领）
审阅人：待定（未分配独立审阅人，不自批为独立验收）
基线 commit：`9239382d8287de313f21cf608c1a21af1370dd30`（认领时的 `feature/user-direction` HEAD，不是已发布共同基线）
分支与最终 commit：`feature/user-direction` / `9901f170f939e8f302ee8f528700fc2bc3a42503`（tree `26fe71f6a74a2b2a01025d70ea7c59f2bb2e22ca`）
认领状态：claimed → **review**

## 触发场景与行为变化

- 输入/触发：任意任务进入规划阶段（`RuntimeController._plan`）——是否走「已注册工作流」还是「动态规划（ReAct）」由主链决定。
- 修改前：
  - 主链是**静态拍板**：专家登记了 `workflow_ids` 且任务意图命中就直接取 `workflow_ids[0]`，不做五项检查（输入/权限/依赖/模型版本/预算），不写理由、不留拒绝证据；
  - `WorkflowMatcher.select`（ALN-07-W）只在**评测**（`evolution/routing_eval.py` 的 `WorkflowModeEvaluator`）里被调用，`routing_eval.py` 自述「主运行时尚未自动接线」；
  - 执行路径与选择理由无关联，无法核对「决策 vs 实际执行」。
- 修改后（新增 `evoagent/routing/mode_router.py` + 包，controller 精确改动点见下）：
  - `ModeRouter.decide` 与评测走**同一入口** `WorkflowMatcher.select`：候选来自专家 `workflow_ids`（任务已钉住工作流时只以该工作流为候选），上下文由任务真实输入（`runtime_manifest["match_inputs"]`）、专家工具白名单、模型版本、`BudgetController.snapshot()` 构建；**预算不可知时不得假设充足**（`budget_known=False` 会让候选被拒并写进理由）。
  - 选择结果（mode/workflow_id/version/rationale/verdicts）写入 `task.runtime_manifest["mode_selection"]`，每个被拒候选的缺失项写入 `mode_rejections`，并发 `execution_mode_selected` 事件（含 rejected 列表）。
  - `_plan`：五项检查全过才设 `workflow_ref` 走工作流；否则明确降级 `dynamic_planning` 并记 `execution_mode`；**已钉住但不可用的工作流显式拒绝**（`workflow_ref_rejected` + `workflow_ref_rejected` warn 事件），清空引用后走动态规划，绝不旁路使用。
  - `_run` 派发前记录 `mode_attribution`（决策模式/工作流 vs 实际执行模式/工作流 + `consistent`）：不一致发 `execution_mode_mismatch` warn 事件并写审计，不静默掩盖。

## 范围

- 独占模块：`evoagent/routing/__init__.py`、`evoagent/routing/mode_router.py`（新增；卡片写入边界 `evoagent/routing/` 此前不存在，本卡建立）。
- 共享文件补丁（精确修改点）：
  - `evoagent/core/controller.py`：`_mode_router` 注入点（构造参数 + 默认自建）、`_plan` 改为显式选择、`_run` 派发前归因、新增 `_select_execution_mode` / `_record_mode_attribution` 两个方法；**未改**任务/状态机/预算/工具/ReAct 既有逻辑。
  - `workflows/engine.py`：**未改**（本卡只决定「用不用工作流」，不改 DAG 执行）。
- 新增/修改契约：运行清单新增 `mode_selection` / `mode_rejections` / `execution_mode` / `workflow_ref_rejected` / `mode_attribution` 五个字段；新增事件 `execution_mode_selected`、`workflow_ref_rejected`、`execution_mode_mismatch`；无数据库 schema 变更、无迁移。
- 兼容性：专家未登记工作流、任务未钉工作流时候选为空 → 与改前一样走动态规划（理由新增可查）；钉住工作流时保持既有 API/恢复语义（只以该工作流为候选）。

## 验证证据

- 正常路径及实际产物：
  - 主链选中工作流：候选五项全过 → `mode_selection.mode=stable_workflow`、`workflow_ref` 生效、任务真实走工作流执行、`mode_attribution.consistent=True`、`execution_mode_selected` 事件带 workflow_id。
- 拒绝/降级/归因：
  - 不可用工作流（`input_schema.required=["theme"]` 且任务未提供）：`mode=dynamic_planning`、`mode_rejections` 含 `inputs:theme`、真实执行走动态规划、归因一致；**没有**静默使用该工作流。
  - 已钉住的工作流不可用：`workflow_ref_rejected` 记录被拒引用与理由、`workflow_ref=None`、`execution_mode=dynamic_planning`。
  - 预算不可知（无预算对象）：`budget_known=False` → 候选被拒、理由含「预算不可知」，模式为动态规划。
  - 归因不一致：人为把决策改成工作流而实际走动态规划 → `consistent=False` + `execution_mode_mismatch` warn 事件（可审计）。
  - 入口一致：同一上下文下 `ModeRouter.decide` 与直接 `WorkflowMatcher(candidates).select` 结果 `to_dict()` 全等。
- 命令、结果、日志与对应源码 commit：
  - 提交 `9901f170f939e8f302ee8f528700fc2bc3a42503`；
  - 本卡专项：`python -m pytest tests/test_execution_mode_routing.py -q` → **7 passed**；
  - 工作流/控制/路由大块回归（workflow/api/control/execution_boundaries/routing_eval/workflow_matching 等 15 个文件）→ **189 passed（34.14s）**，含既有「钉住工作流未知副作用必须挂起」「等待工作流授权」两条边界用例；
  - 全量后端：`python -m pytest tests/ -q` → **1892 passed / 0 failed / 1 skipped（391.81s）**，日志 `.planning/2026-10-25-aln08w/backend-tests.txt`。
- Mock / 本地组件 / 真机 / 真实供应商的验证边界：
  - 本轮为**本地路由夹具**（本地注册工作流定义 + Mock 工具/模型 + 真实 SQLite/预算控制器），不是生产工作流资产、不是真机、不是真实供应商；
  - `cost_estimate` 用节点数作量级近似（合同单位「次」），未接真实成本档案（属 ALN-08-P）；
  - `hybrid_agent` 的「动态节点补齐」尚未在执行引擎落地：选择层可产出 hybrid，若候选声明 `dynamic_nodes`，真实执行仍按完整 DAG 跑，归因会把决策(hybrid) 与实际(stable_workflow) 标为不一致并告警——这是如实暴露限制，不是伪通过。

## 回滚与未完成事项

- 回滚触发、操作及验证：`git revert 9901f170f939e8f302ee8f528700fc2bc3a42503`（删新增 routing 包与测试、恢复 controller 静态选择；运行清单多出的字段/事件对旧代码无副作用）。回滚后 `tests/test_execution_mode_routing.py` 整体消失，`tests/test_execution_boundaries.py` 与工作流测试应仍全绿。
- 保留的限制、后续 ALN 和依赖：
  - 未接生产工作流资产目录与真实成本档案（候选 `cost_estimate` 为节点数近似）；
  - `hybrid_agent` 动态节点执行未落地（见上），属工作流引擎后续范围；
  - 依赖 ALN-23-A（任务契约对外模型，review）：本卡只读既有 `task.user_goal/intent/runtime_manifest`，若 23-A 定稿调整字段语义需复核本卡映射；
  - 未做长时间/高并发路由压测；未把 `mode_selection` 暴露到 API/客户端视图（属 23-D/24-C 等卡）。
- 影响的主题/实体/ADR/契约/系统验收条目：新增运行清单五个路由字段与三个事件；工作流执行语义与 ReAct 语义均不变（只改「选谁」的可解释性与证据）；ALN-08 整包关闭仍需真实评测集与价格档案（ALN-08-P 已交付结构，真实数据待接）。

## 审阅结论：待审

## 整包结论：仍实现中（ALN-08：真实工作流资产、动态节点执行与生产评测数据仍待验；本卡交付主链模式选择、拒绝证据与执行归因）
