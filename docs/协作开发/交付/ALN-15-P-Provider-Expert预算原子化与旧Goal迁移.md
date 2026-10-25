# ALN-15-P：Provider/Expert 预算原子化与旧 Goal 迁移

作者 opencode-evo；待独立复核，不关闭 ALN-15 整包，不发布基线。

功能提交见 workboard `delivered_commit`；本卡依赖 ALN-15-R（accepted）的权威账户、
`identity/micros/limits` 口径与 `_legacy_goal_history` 门。契约沿用
[运行时预算接线 v1](../设计/ALN-15-R-运行时预算接线契约.md)，不改变其语义。

## 交付行为

### 1. Provider/Expert 池的原子预留

旧池（`budget_ledger` 的 `scope:day/month` 记账 + `BudgetLedger.check` 的“先查后账”）
在多任务并发时允许同一额度被重复合法消费。新增 `evoagent/core/pool_reservations.py`：

- 每个池按周期建立**不可变**的生命周期账户，命名
  `pool-v1:<scope>:<period>:<stamp>`（`kind=pool`，scope 限 `provider:`/`expert:`）。
  日期/月份切换即新账户，旧账户已花用量永久保留（15-A 契约：周期不重置花费）。
- 每次模型调用前在同一 SQLite `BEGIN IMMEDIATE` 事务内持额
  （费用上界 = `min(池日上限, 单次调用上限)`，调用次数 1 次）；
  并发线程/进程竞争同一池时，不允许双重占用。
- 无正数费用上界（`max_cost` 缺失/为 0）或单次调用上限为 0 的池**拒绝原子化**并
  回退旧检查——不假定零费（与 15-R「未知价格无宿主正数上界时发送前拒绝」同语义）。
- 未知成本结算走 `mark_unknown`：持续占用上界直到显式对账，绝不按 0 元释放。
- 未下发即过期 → 退款；已下发未结算 → 未知（对账前持续占用），与 15-A
  `recover_expired` 状态机一致。重复 `check` 复用同一笔未下发持额（保持旧
  check 的幂等语义，不让每次检查都新建/退款）；仅持额已过期才退款换新；
  已下发未结算的保留给 TTL 扫描转 unknown（不吞掉真实调用）。

`BudgetController` 接线（`evoagent/core/budget.py`，不改 controller.py）：

- `_check_pools` 先尝试 `_request_pool_hold`（幂等 `prepare` 周期账户 + 原子持额），
  不可原子化时回退原 `ledger.check`——旧池报表/镜像记账语义不变。
- `record_model_call` → `_dispatch_pool_holds`（一次性领取，崩溃窗口后按未验证对账）；
  `settle_model_cost` → `_settle_pool_holds`（金额结算）；`mark_unknown_model_cost` →
  unknown 挂账。结算异常交给 TTL 扫描，不在调用路径抛错掩盖真实用量。
- `BudgetLedger.pool_reservations()` 是 opt-in 访问器（无 `Database` 的内存账本
  保持旧检查语义）。

### 2. 旧 Goal 历史用量迁移

15-R 的 `_legacy_goal_history` 在绑定前拒绝“存在历史支出的未迁移 Goal 家族”，
但没有迁移工具。新增 `evoagent/goals/budget_migration.py`：

- `migrate(goal_id)`：把家族内所有未绑定 runtime 账户的旧任务
  `execution_budget_state` 用量，聚合记入 15-R 的 `identity("goal", goal_id)`
  权威账户（预留沿账户父子链逐级扣减）；每任务写一条 binding 标记行，使
  `_legacy_goal_history` 的 `b.task_id IS NULL` 门放行、重复绑定不重放历史。
  标记行包含 `schema_version=1 / account_id / additional_accounts=[] / goal_ids /
  task_id`，**不含 `legacy_usage`**（防 `import_legacy` 二次计费）。
- 幂等：预留幂等键 `goal-migration:v1|<goal_id>|<task_id>`，migrate 重跑零增量。
- 已知成本先迁、未知成本后迁（纯未知任务用剩余可用额度作保守上界）；若家族额度
  不足以给未知任务正数上界，列入 `reconciliation_required` 而不静默丢弃/假定零费。
- `verify(goal_id)`：已知成本任务的 Goal 账户 spent 与旧用量总和精确比对，
  未知成本任务单独汇报。
- `rollback(goal_id)`：仅对**未下发**的迁移预留退款；账户、账本事件与 binding
  标记保留（账户不可变，真实历史用量不撤销）。

## 源码与迁移

新增：

- `evoagent/core/pool_reservations.py`
- `evoagent/goals/budget_migration.py`
- `tests/test_budget_pool_atomization.py`（15 项）
- `tests/test_goal_budget_migration.py`（7 项）

修改：

- `evoagent/core/budget.py`：`BudgetLedger.pool_reservations()`；
  `BudgetController._pool_holds` / `_request_pool_hold` / `_pool_service` /
  `_dispatch_pool_holds` / `_settle_pool_holds` 及 record/settle/unknown 钩子。
- `evoagent/goals/__init__.py`：导出 `GoalBudgetMigration`。

共享文件登记：`evoagent/core/budget.py`、`evoagent/models/metering.py`、
`evoagent/goals/store.py`（本卡只改 `budget.py` 与本包新增文件；
`metering.py`/`store.py` 未改——`settle_adapter_cost` 已走
`mark_unknown_model_cost`/`settle_model_cost`，池侧联动在 `BudgetController` 内完成，
计量层无需桥接）。不新增数据库迁移（复用 15-A/15-R 既有域表）。

## 验证记录

环境：Windows / Python 3.10 / 本仓库；命令均带 `-X utf8`。

1. 专项（本卡新增）：

```
python -X utf8 -m pytest tests/test_budget_pool_atomization.py tests/test_goal_budget_migration.py -q --no-header
22 passed（池原子化 15 + Goal 迁移 7）
```

2. 预算域回归（不得放宽 legacy 断言）：

```
python -X utf8 -m pytest tests/test_budget_hierarchy.py tests/test_runtime_reservations.py \
  tests/test_budget_reservations.py tests/test_budget_pool_atomization.py \
  tests/test_goal_budget_migration.py -q --no-header
110 passed
```

3. 隔离全量回归（干净 `d1272c9` 树 + 仅本卡 4 个源文件与 2 个测试文件）：

```
python -X utf8 -m pytest tests -q --no-header
1723 passed, 1 skipped in 369.32s
```

4. 共享工作区全量（对照，含其他协作者未提交的在途改动，不作本卡结论）：

```
1733 passed, 18 failed
```

失败全部位于他人文件域（`tests/test_developed_review.py` 10 项、`tests/test_feedback.py`
7 项、`tests/test_goal_opportunities.py` 1 项与
`tests/test_runtime_reservations.py::test_explicit_hint_does_not_allocate_a_phantom_classifier_call`），
原因是工作区中 `evoagent/api/feedback_api.py`、`evoagent/core/controller.py` 等
未提交改动；隔离树（干净 HEAD + 本卡改动）对照 159/159 通过，其中包含上述全部
文件域的关联测试。本卡改动不触及这些文件。

全链探针（Mock/夹具口径，非真机）：并发满额持额无超留、settle 转 spent 并释放、
满额第三次拒绝、unknown 挂上界不假定零费、重复 check 复用同一持额（旧 check
幂等语义）、过期未下发持额退款换新、已下发持额不被退款、跨控制器不越权退款、
TTL sweep 未下发→refunded、成功回滚部分成功的多池检查、内存账本回退旧检查、
旧 `provider:<name>` scope 镜像记账不变。

## 兼容/迁移影响与保留缺口

- 兼容：旧池绑定（`bind_pool` 的 scope/period 参数）与 `budget_ledger` 报表不变；
  无 `Database` 的内存账本完全走旧检查。旧池 cap 缺正数费用上界时保持旧语义
  （这是有意的回退，不是回归）。
- 迁移：旧 Goal 迁移是显式运维动作（`GoalBudgetMigration.migrate`），未在启动时
  自动执行；重复执行幂等，回退只撤未下发预留。
- 保留缺口：
  1. 未接真实供应商账单对账（unknown 挂账需运维显式 `reconcile`）。
  2. 迁移 binding 标记与 15-R `bind()` 生成的 task 账户并存两个口径；任务首次
     运行时绑定命中迁移标记、以 Goal 账户为权威账户，需在后续整包联调中确认
     所有 ceilings/报表路径一致（已在单测覆盖额度链，未覆盖完整控制器运行）。
  3. 并发不超留的进程级验证以单库多连接线程夹具覆盖；多进程竞争沿用 15-A
     `BEGIN IMMEDIATE` 底座，未在本卡重复多进程实测。
  4. 未知成本任务在迁移后持续占用剩余上界，需显式对账后才释放；对账入口仍为
     15-A 的 `reconcile`。

## 结论

本卡交付 provider/expert 原子持额与旧 Goal 迁移工具；`status` 转 `review`，
审阅人未定保留 null，不自批为独立验收；发布决定另行处理。
