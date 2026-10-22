# ALN-08-M 交付记录：路由覆盖复核与可对账的模型计量

负责人：claude-main（本会话）
审阅人：待定（未编造）
基线 commit：尚未发布（workboard baseline=candidate，当前 HEAD `23d3950`）
分支与最终 commit：工作区改动，未提交
认领状态：in_progress（本卡行为已实现，待审阅；整包 ALN-08 未关闭）

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
    不允许把路由自己的评分输出当答案。

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
  - `tests/test_model_metering.py`、`tests/test_routing_eval.py`、`tests/test_routing_eval_api.py`
- 共享文件补丁与整合角色：无。`core/budget.py`、`react/engine.py`、`core/contracts.py`、持久化与装配均未改动。
- 新增/修改契约：`ModelUsage` / `cost_source` / `token_source` / `pricing_known`。提供方为 models 线；消费方为运行账单（待接线）与路由报告。
- 数据迁移：无 schema 变更；事件负载为增量字段；旧档案缺省 `pricing_known=False`，行为从“隐式免费”收紧为“未知”。

## 验证证据

- 正常路径：`tests/test_model_metering.py`（20 项）覆盖提供方用量采集、按 token 计费、Mock 显式免费、预估标记、账本对账与按模型归因。
- 拒绝/失败：同文件覆盖 429 限流（3 次有界重试）、超时（有界）、400 供应商错误（不重试）、失败调用不产生伪造用量；事件总线故障不影响调用。
- 覆盖矩阵：`tests/test_routing_eval.py::TestFiveLayerCoverage` 锁定五层顺序、已评测层有指标、缺失层必须写明缺口；`tests/test_routing_eval_api.py` 覆盖 `GET /routing/coverage` 与评测响应中的 `coverage`。
- 模型层：`TestModelRoutingEvaluation`（6 项）覆盖显式/默认/评分/成本优先/视觉约束/无候选回退与费用对账；`/routing/model-eval` 端到端验证准确率、失败清单与缺期望值的 400 拒绝。
- 命令与结果：
  - `python -m pytest tests/test_model_metering.py -q` → 20 passed
  - `python -m pytest tests/test_routing_eval.py tests/test_routing_eval_api.py -q` → 43 passed
  - `python -m pytest tests -q` → **784 passed, 1 skipped**（含并行会话已合入的 E-12/E-13/ALN-04 测试）
- 验证边界：全部为本地确定性夹具与 Mock；**未**调用真实供应商、未验证真实计费单、未做真机/生产部署验收。

## 回滚与未完成事项

- 回滚：还原上述 models/routing 文件与测试即可；无迁移、无持久化格式变更。事件字段为增量，旧消费者不受影响。
- 保留限制：
  - 工作流层、执行模式层当前没有路由入口，覆盖矩阵已显式标为缺口；
  - 模型层已有确定性评测与费用对账，但评测用例需调用方提供期望模型，尚无生产评测集；
  - 实测成本尚未写入 `BudgetController`/任务账单：`react/engine.py` 与
    `core/budget.py` 仍有运行内核线未提交改动，需待其合入后按接口串行接线；
  - 调用尚未回填 `task_id`/`run_id`；`ModelUsage` 已预留字段与 `call_id`；
  - 真实供应商价格档案未配置，生产默认仍为 unknown（这是刻意的诚实缺省）。
- 影响主题：ALN-08（整包未关闭）、R-03/F-53（路由评测）、ALN-15（预留/结算接口待评审）。

审阅结论：待审
整包结论：仍实现中（本卡行为完成，不关闭 ALN-08）
