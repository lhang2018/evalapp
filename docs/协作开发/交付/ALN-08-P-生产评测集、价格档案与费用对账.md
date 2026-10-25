# ALN-08-P 生产评测集、价格档案与费用对账

任务：ALN-08-P / 生产评测集、价格档案与费用对账
负责人：dsh-lead（本会话认领）
审阅人：待定（未分配独立审阅人，不自批为独立验收）
基线 commit：`17854f2dc93cec32a3fb845a650838d9dff2821b`（认领时的 `feature/user-direction` HEAD，不是已发布共同基线）
分支与最终 commit：`feature/user-direction` / `957369be958691748430db8b6a1c4c5b9c7f5d9e`（tree `501d8d84592d7296236fdab71ff3fd0ad334f0bf`）
认领状态：claimed → **review**

## 触发场景与行为变化

- 输入/触发：
  - 需要按供应商价格给一批模型调用定价/算账；
  - 需要用供应商账单核对内部计量，解释差异；
  - 需要一套有来源、已脱敏、覆盖可说明的代表性任务集做评测。
- 修改前：
  - `models/metering.py` 只有「档案里写死的单价 + 未知即 None」的计量口径，没有**价格档案**（无来源、无有效期、无币种、无重叠校验），也没有**账单对账**（内部计量 vs 供应商账单的逐行差异原因），评测集只有内置数据集，没有「来源/脱敏/覆盖」声明结构。
- 修改后（新增 3 个模型域模块 + 1 个 schema 域模块；`core/budget.py`、`models/metering.py` **一行未改**）：
  - `evoagent/models/pricing.py`：
    - `PriceEntry` 强制**来源**（`source_ref`：供应商价目/账单编号）、**有效期**（`effective_from/effective_to`）、3 位币种、非负有限单价；`PriceCatalog.register` 拒绝同模型同币种**生效区间重叠**、拒绝同 id 改内容（价格档案不可变）。
    - `PriceCatalog.cost/price_for`：按时间点解析，状态只有 `priced` / `free`（显式 0 价）/ `unknown:no_price` / `unknown:not_yet_effective` / `unknown:expired` / `unknown:invalid_usage`；**未知返回 `cost=None`，绝不造金额**。
    - `PriceCatalog.aggregate`：金额只累加 `priced`（`free` 记 0 计数），未知行只进 `unknown_count`，`unknown_amount` 恒为 `None`。
    - SQLite 持久（`model_prices`）：重启后同一批用量可重新解析 → 「未知费用的后续对账」路径成立。
  - `evoagent/models/billing.py`：`reconcile(metering, bill)` 按 `request_ref` 逐行对账，原因码固定为 `matched` / `rounding`（容差内 ≤0.01）/ `amount_mismatch` / `currency_mismatch` / `invoice_missing_line` / `bill_only_line` / `unknown_price` / `duplicate_bill_line`；**只有 matched 与容差内 rounding 放行**，其余全部进 `followups`；未知价格行只计 `unknown_count`、不填金额、不参与金额比较。
  - `evoagent/models/tasksets.py`：`RepresentativeTaskSet` 强制 `source_ref`（脱敏批次/工单）+ `anonymized=True` + 脱敏方法声明 + 唯一用例 ID；样本正文命中口令/令牌/证件等敏感标记直接拒绝；`coverage()` 给出领域/意图/场景分布与 `missing_domains`；快照持久在 `task_sets` 表，可整包读写。
  - `evoagent/core/schema/cost_catalog.py`：新域模块（幂等建表 `model_prices`、`task_sets`，无版本化迁移）。

## 范围

- 独占模块：`evoagent/models/{pricing,billing,tasksets}.py`、`evoagent/core/schema/cost_catalog.py`（新增）。
- 共享文件补丁：**无**。本卡 shared_files 登记的 `evoagent/core/budget.py`、`evoagent/models/metering.py` 一行未改——复用既有 `ModelUsage.cost=None 表示未知` 口径，本卡只补「价格从哪来/怎么对账/评测集怎么声明」。
- 新增契约：价格档案记录与解析状态、对账结果结构（`Reconciliation.to_dict()`）、任务集快照结构；无数据库迁移、无既有行为变更。
- 数据迁移/兼容：不涉及；未知费用语义与既有 metering 一致（None ≠ 0）。

## 验证证据

- 正常路径及实际产物：
  - 价格在有效期内解析正确：输入 2000 / 输出 1000 tokens，单价 0.5/1.5 每 1K → `2.5`，并回传 `price_id` 与 `source_ref`；显式 0 价 → `status=free, cost=0.0`（不是 unknown）。
  - 对账全匹配 → `passed=True`、`explanations={"matched":2}`、`followups=[]`；0.004 舍入差 → `rounding` 仍通过。
- 拒绝/失败/未知/重启：
  - 缺 `source_ref`、负单价、`effective_to<=effective_from`、非法币种、同模型同币种区间重叠、同 `price_id` 改内容 → 全部 `PriceError` 拒绝（相邻区间、不同币种允许）。
  - 价格未生效（`not_yet_effective`）/已过期（`expired`）/无该模型价格（`no_price`）→ `cost=None`；未知行聚合只进 `unknown_count`，`unknown_amount=None`。
  - 账单缺行 / 仅账单行 / 币种不符 / 金额超容差 / 重复账单行 / 未知价格 → 各自原因码 + `followups`，`passed=False`；未知价格行的账单金额不参与金额比较（不虚增 `delta`）。
  - 重启（新 `Database` 连接）后价格档案仍可读并解析出金额。
  - 任务集：缺来源、未声明脱敏、未写脱敏方法、空集、重复用例、样本含 `password/token/证件` 等标记 → 全部拒绝；合法集 `coverage()` 给出领域分布与缺口；快照 save/load 往返一致，缺失版本报错。
- 命令、结果、日志与对应源码 commit：
  - 提交 `957369be958691748430db8b6a1c4c5b9c7f5d9e`；
  - 本卡专项：`python -m pytest tests/test_cost_governance.py -q` → **13 passed**；
  - 计量/预算既有回归：`tests/test_metering_budget.py tests/test_budget.py` → **13 passed**；
  - 本会话相关域回归（08-P + 10-E + 02-P + 09-H + 13-P）：**53 passed（21.11s）**；
  - 全量后端：`python -m pytest tests/ -q` → **1868 passed / 0 failed / 1 skipped（359.61s）**，日志 `.planning/2026-10-25-aln08p/backend-tests.txt`（本会话首次全量零失败）。
- Mock / 本地组件 / 真机 / 真实供应商的验证边界：
  - 价格与账单数据均为**明确标注的夹具**（`source_ref="vendor:pricing/2026-10"` 等测试值），不是真实供应商合同价或真实发票；
  - 代表性任务集验证的是**结构与校验**（来源/脱敏声明/覆盖统计），样本为示例文本，不是真实生产任务脱敏产物；
  - 未接真实供应商账单 API、未接 `core/budget.py` 的运行时未知费用入账（本卡只提供解析与对账接口；接线属预算线与发布线）。

## 回滚与未完成事项

- 回滚触发、操作及验证：`git revert 957369be958691748430db8b6a1c4c5b9c7f5d9e`（删新增模块与测试；`model_prices`/`task_sets` 表可保留，幂等建表、无迁移）。回滚后 `tests/test_cost_governance.py` 整体消失，既有计量/预算行为不变。
- 保留的限制、后续 ALN 和依赖：
  - 未接真实供应商价目/发票（需替换 `source_ref`、币种与税率口径，并按供应商 API 做拉取与签名校验）；
  - 未把价格档案接入运行时成本结算（`metering`/`budget` 仍用 `ModelProfile` 单价；接入需预算线排期，属 ALN-15 相关）；
  - 未做多币种换算（只做同币种对账，跨币种按 `currency_mismatch` 后续项处理）；
  - 对账输入是逐行结构，未接 `UsageLedger` 自动导出账单行（接线属成本报表/发布线）；
  - 代表性任务集未接真实评测流水线（`evolution/evaluation` 消费需另行映射）。
- 影响的主题/实体/ADR/契约/系统验收条目：新增 `model_prices`、`task_sets` 表与价格/对账/任务集三份结构契约；既有 `ModelUsage.cost=None` 未知语义保持不变；ALN-08 整包关闭仍需真实价格/账单与生产评测集。

## 审阅结论：待审

## 整包结论：仍实现中（ALN-08：真实供应商价格/账单接入、运行时结算与生产评测集仍待验；本卡交付价格档案、对账与任务集结构）
