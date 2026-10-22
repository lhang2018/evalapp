# ALN-08-M 交付记录：路由覆盖复核与可对账的模型计量

负责人：claude-main（本会话）
审阅人：codex-runtime（对 claude-main 原交付交叉审阅；本轮审阅修复仍需其他人复核）
基线 commit：尚未正式发布（workboard baseline=candidate）；干净准备候选见 prepared_candidate
分支与最终 commit：原交付已在 `27784ab`/`ade7d3e`/`4065d52` 提交
认领状态：review（原交付已审出问题并修复，待修复复核；整包 ALN-08 未关闭）

交叉审阅续记：并发计量串用、非法用量/价格、未知价格路由优势、不可用/能力不符回退及错误信息回显已在 `d77b241` 修复。下文为首轮交付证据；当前状态以[交叉审阅记录](ALN-06-08-X-交叉审阅与基线准备.md)为准。

## 触发场景与行为变化

- 输入/触发：真实模型或 Mock 经 `MeteredModel` 执行 `generate` / `suggest_action`；路由评测经 `RoutingEvaluator` 与 `/routing/eval`。
- 修改前：
  - `OpenAICompatAdapter` 丢弃提供方返回的 `usage`，只用字符数估算成本；
  - 估算值直接作为预算记账，无法区分预估与实测；
  - 价格缺失（`price_per_1k_* = 0` 且无标记）被隐式当作免费；
  - 路由只有意图/专家两层指标，五层覆盖情况没有结构化结论。
- 修改后：
  - 采集提供方 `prompt_tokens` / `completion_tokens`（兼容 `input_tokens` / `output_tokens`）与请求号；
  - `ModelUsage` 明确标记 `token_source`（provider/estimate）与 `cost_source`（provider/estimate/free/unknown）；
  - 价格未知时 `cost=None`，绝不按 0 元记账；只有显式 `pricing_known=True` 且价格全 0 才记为 free；
  - `UsageLedger` 对账：同一 `call_id` 的实测覆盖预估值，不重复计费；未知成本单独计数；
  - `model_call` 事件负载新增 token/成本/来源/请求号字段（向后兼容，原字段保留）；
  - `routing_coverage()` 输出五层（意图/专家/工作流/执行模式/模型）的实现、入口、评测与缺口；
  - `GET /routing/coverage` 与 `POST /routing/eval` 响应新增 `coverage`；
  - 模型层补上评测：`ModelRoutingEvaluator` 走真实 `ModelRouter.decide`，产出
    路由准确率/回退率/视觉违规/by_kind/by_source/per_model，并可附 `UsageLedger`
    的实测与未知成本对账；`POST /routing/model-eval` 由调用方提供期望模型，
    不允许把路由自己的评分输出当答案；
  - 实测成本接入任务预算：ReAct 运行内核优先读取本次调用的 `ModelUsage.cost`
    结算（预登记不重复计费），成本未知时只累加 `unknown_model_costs`，
    **不按 0 元记账**；无计量结果的旧适配器保留字符数预估回退；
  - `BudgetUsage` 新增 `unknown_model_costs`（随用量持久化/恢复），
    `CostReporter` 在总计与按任务明细中如实暴露该计数；
  - `model_call` 事件按当前执行租约回填 `task_id`/`run_id`（事件行与
    负载同时写入），计量记录因此可按任务/运行检索；无租约的独立调用
    保持为空，不臆造关联。

## 范围

- 独占模块（本卡直接修改）：
  - `evoagent/models/base.py`（`pricing_known` 字段，默认未知）
  - `evoagent/models/metering.py`（计量记录/对账/成本来源）
  - `evoagent/models/openai_compat.py`（用量与请求号采集）
  - `evoagent/models/mock.py`（显式免费）
  - `evoagent/models/router.py`（`decide()` 返回决策来源，`route()` 行为不变）
  - `evoagent/models/__init__.py`（导出）
  - `evoagent/evolution/routing_eval.py`（五层覆盖复核、模型层评测）
  - `evoagent/api/routing_eval_api.py`（覆盖矩阵与模型层评测端点）
  - `evoagent/core/budget.py`（共享 integration 补丁：未知成本计数与报告字段）
  - `evoagent/react/engine.py`（共享 runtime 补丁：按计量结果结算）
  - `tests/test_model_metering.py`、`tests/test_routing_eval.py`、
    `tests/test_routing_eval_api.py`、`tests/test_metering_budget.py`
- 共享文件补丁与整合角色：`core/budget.py`（integration）与 `react/engine.py`
  （runtime）为跨线补丁，**需对应领域 reviewer 审阅**；补丁均为增量字段/
  分支，未改动既有方法签名与默认行为。`core/contracts.py`、持久化与装配未改动。
- 新增/修改契约：`ModelUsage` / `cost_source` / `token_source` / `pricing_known`；
  `BudgetUsage.unknown_model_costs` / `BudgetController.mark_unknown_model_cost`。
  提供方为 models 线；消费方为运行账单与路由报告。
- 数据迁移：无 schema 变更；事件负载与预算用量均为增量字段；旧档案缺省
  `pricing_known=False`，行为从“隐式免费”收紧为“未知”；旧用量记录缺省
  `unknown_model_costs=0`。

## 验证证据

- 正常路径：`tests/test_model_metering.py`（20 项）覆盖提供方用量采集、按 token 计费、Mock 显式免费、预估标记、账本对账与按模型归因。
- 拒绝/失败：同文件覆盖 429 限流（3 次有界重试）、超时（有界）、400 供应商错误（不重试）、失败调用不产生伪造用量；事件总线故障不影响调用。
- 覆盖矩阵：`tests/test_routing_eval.py::TestFiveLayerCoverage` 锁定五层顺序、已评测层有指标、缺失层必须写明缺口；`tests/test_routing_eval_api.py` 覆盖 `GET /routing/coverage` 与评测响应中的 `coverage`。
- 模型层：`TestModelRoutingEvaluation`（6 项）覆盖显式/默认/评分/成本优先/视觉约束/无候选回退与费用对账；`/routing/model-eval` 端到端验证准确率、失败清单与缺期望值的 400 拒绝。
- 预算接线：`tests/test_metering_budget.py`（6 项）锁定实测成本优先于预估、
  未知成本只计数不按 0 元记账、旧适配器回退预估、预登记不重复计数、
  未知计数随用量恢复并在作用域预算向上传播，以及真实任务执行链中
  `model_call` 事件按 task/run 可检索。
- 执行归因：`tests/test_model_metering.py::TestExecutionAssociation`（2 项）
  锁定有租约时回填 task/run、无租约时不臆造。
- 命令与结果：
  - `python -m pytest tests/test_model_metering.py -q` → 22 passed
  - `python -m pytest tests/test_routing_eval.py tests/test_routing_eval_api.py -q` → 43 passed
  - `python -m pytest tests/test_model_metering.py tests/test_metering_budget.py
    tests/test_routing_eval.py tests/test_routing_eval_api.py -q` → 71 passed
  - `python -m pytest tests -q` → **816 passed, 1 skipped, 2 failed**；
    失败为 `test_ledger_drift`（FEATURES/GAP 台账引用漂移）与
    `test_aln10_completion`（并行 ALN-10 会话在途），均不在本卡改动范围。
- 验证边界：全部为本地确定性夹具与 Mock；**未**调用真实供应商、未验证真实计费单、未做真机/生产部署验收。

## 回滚与未完成事项

- 回滚：还原上述 models/routing/budget 文件与测试即可；无迁移、无持久化格式变更。
  事件与预算字段均为增量，旧消费者与旧用量记录不受影响。
- 保留限制：
  - 工作流层、执行模式层当前没有路由入口，覆盖矩阵已显式标为缺口；
  - 模型层已有确定性评测与费用对账，但评测用例需调用方提供期望模型，尚无生产评测集；
  - 实测成本已进入任务预算与 `CostReporter`，但未知成本只做计数，
    尚未按估算区间写入预算池（不臆造金额）；
  - `task_id`/`run_id` 依赖执行租约；无租约的独立模型调用（如直接分类
    端点）保持为空，这是刻意保留的诚实缺省；
  - 真实供应商价格档案未配置，生产默认仍为 unknown（这是刻意的诚实缺省）。
- 影响主题：ALN-08（整包未关闭）、R-03/F-53（路由评测）、ALN-15（预留/结算接口待评审）。

审阅结论：原交付交叉审阅已完成并发现缺陷；修复待其他审阅人复核，不自批修复。
整包结论：仍实现中（本卡行为完成，不关闭 ALN-08）
