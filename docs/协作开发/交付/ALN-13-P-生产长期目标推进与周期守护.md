# ALN-13-P 生产长期目标推进与周期守护

任务：ALN-13-P / 生产长期目标推进与周期守护
负责人：dsh-lead（本会话认领）
审阅人：待定（未分配独立审阅人，不自批为独立验收）
基线 commit：`41fc372cf7e7d38bc27679f5e91ccc0844c7d091`（认领时的 `feature/user-direction` HEAD，不是已发布共同基线；基线仍为 candidate，prepared_candidate `21899e6` 未发布）
分支与最终 commit：`feature/user-direction` / `78a70f15ce5b18dd7c6e0accd4151023ae60833c`（tree `4ff9a45bff19ca5776824daaff9f062640057803`）
认领状态：claimed → **review**

## 触发场景与行为变化

- 输入/触发：
  - `GoalScheduler.tick()`（可由 `start()` 后台线程周期调用，或由集成角色在调度点调用）；
  - 活跃 Goal（`status ∈ {ready, running}`）的到期槽位；主任务负载与每轮额度决定本轮是否让出/推进多少。
- 修改前：
  - 13-A/14-A 已有 `GoalGovernor.advance()`（**一次性**、人工/API 触发）与 `report/reconcile`（无进展、失败、预算、状态重复等停止条件），但没有「谁按周期调用」、没有调度状态持久化、没有多进程互斥、也没有「主任务忙时让出」的负载守护；重启后无法恢复「下一轮什么时候该跑」。
- 修改后（只加不改已接受切片）：
  - `evoagent/goals/scheduler.py::GoalScheduler`：
    - **周期**：每个非终态 Goal 持久登记 `next_due_at`（新表 `goal_schedules`），`tick()` 只推进到期目标；`command_id = f"{prefix}:{goal_id}:{时间槽}"`，同一槽位重放会命中 `advance` 的幂等表，返回既有结果而不是重复派发（崩溃恢复安全）。
    - **负载守护**：`primary_load()` 统计 `origin ∈ {user, scheduled}` 的未完成主任务；`busy >= primary_busy_limit` 时本轮直接返回 `reason="primary_load_guard"` 并发出 `goal_schedule_yielded` 事件——长期自主目标不会饿死用户任务；派生推进另有 `max_goals_per_tick`/`max_steps_per_goal` 上限。
    - **停止与授权每轮重查**：先 `reconcile()`（14-A 停止条件会把目标置 paused/review），再逐目标 `advance()`；授权撤销/过期/等级不足由 `GoalStore._grants` 每轮重新校验，`PermissionError` 记为 `stopped` 并退避，不缓存旧授权。
    - **多进程**：`goal_schedules` 行内租约 + fencing token（`lease_token`、`attempts` 每次接管 +1）；租约过期可被其他进程接管，旧持有者 `_finish` 抛 `LeaseLost`，不会覆盖新持有者的调度状态。
    - **退避与审计**：失败按 `failure_backoff_seconds × 2^(n-1)`（上限 `max_backoff_seconds`）持久化到 `consecutive_failures`/`last_outcome`；每轮产出结构化 `tick` 报告（advances/skipped/stopped/errors/load）并发 `goal_schedule_tick` 事件。
  - 新增 `evoagent/core/schema/goal_schedule.py` 域模块（`CREATE TABLE IF NOT EXISTS`，自动发现，幂等；按 `migrations.py` 约定**不加版本化迁移**）。

## 范围

- 独占模块：`evoagent/goals/scheduler.py`；`evoagent/core/schema/goal_schedule.py`（新域模块）。
- 共享文件补丁与整合角色：
  - 读取但不修改 `evoagent/goals/{store,governance}.py`（13-A/14-A 已接受切片）；`shared_files` 登记的 `evoagent/goals/store.py` 本卡**未改动一行**；
  - 新增表位于**新建**的域模块（schema 包设计为「新增表 = 新建域模块，零处改动」，避免与 `persistence._init_schema`/既有域文件冲突）；未改 `app.py`/`cli.py`/`api/`（不在本卡写入边界）。
- 新增/修改契约，提供方与消费方：
  - 新表 `goal_schedules`（调度状态 + 租约），新增 `GoalScheduler` 内部接口（`sync/due/tick/stats/start/stop/primary_load`）。
  - 不改变 `GoalGovernor.advance/reconcile` 的接口与语义，也不改变 Goal 十态与授权模型。
- 数据迁移、旧格式兼容或明确拒绝：
  - 旧库自动建表，无迁移、不重建；无 `GoalScheduler` 时既有 Goal/advance/API 行为完全不变。
  - 非法策略参数（非正数、未知字段、`max_steps_per_goal>32`、布尔冒充整数）直接 `ValueError` 拒绝。

## 验证证据

- 正常路径及实际产物：
  - 专项 10 项通过：`python -m pytest tests/test_goal_scheduler.py -q` → `10 passed`；
  - 覆盖：到期目标被推进并落 `goal_schedules`（`next_due_at` 前移、租约清空、`last_outcome.status=advanced`）、重启后同一槽位重放不新增任务（`command_id` 与任务 ID 均相同）、`tick` 报告与事件结构。
- 拒绝/失败/并发/重启：
  - 负载守护：存在未完成 `origin=user` 任务时 `reason=primary_load_guard`、零派发、Goal 仍 `ready`；
  - 停止接入：paused 目标只记录 `inactive:paused` 不派发；撤销授权（`GoalStore.revoke` → blocked）只记录 `inactive:blocked`；`manual-review/v1` 一次推进后 `reconcile` 把目标置 paused，下一轮不再派发；
  - 多进程：租约未过期时第二个调度器领不到（`_claim` 返回 None）；过期后接管且 `attempts` 1→2，旧持有者 `_finish` 抛 `LeaseLost`；
  - 失败退避：`advance` 抛运行时异常 → `tick.ok=False`、`errors` 记录、`consecutive_failures=1`、`next_due_at = at + failure_backoff_seconds`；
  - 重启：新连接 + 新 governor/scheduler 从库读到调度行，按原 `next_due_at` 续跑。
- 命令、结果、日志与对应源码 commit：
  - 提交 `78a70f15ce5b18dd7c6e0accd4151023ae60833c`；
  - 本卡专项：`python -m pytest tests/test_goal_scheduler.py -q` → 10 passed；
  - goal 域回归：`tests/test_goal_governance.py tests/test_goal_opportunities.py tests/test_goal_scheduler.py tests/test_goal_budget_migration.py` → **112 passed（37.96s）**；
  - 全量后端：`python -m pytest tests/ -q` → **1815 passed / 3 failed / 1 skipped（374.70s）**，日志 `.planning/2026-10-25-aln13p/backend-tests.txt`；
  - 本卡改动前同一工作区全量为 `1769 passed / 2 failed / 1 skipped`（09-H 交付时的记录）。
  - 全量中 3 项失败**均不是本卡引入**（本卡只新增 3 个新文件，未修改任何既有模块）：
    - `tests/test_runtime_reservations.py::test_explicit_hint_does_not_allocate_a_phantom_classifier_call`：他人未提交的 `evoagent/core/budget.py`/`core/controller.py` 改动（预算/预留线）；已用「暂存本人改动复跑」在 09-H 报告中归因过，仍失败；
    - `tests/test_ledger_drift.py::TestNoDrift::test_all_ledgers_have_no_drift`：`GAP_ANALYSIS.md:177` 裸引用 `cli.py:232` 因他人 15:27 修改 `evoagent/cli.py` 而漂移（`tools/scan_all_ledgers.py` 1 条 `bare_points_to_blank`），与本卡无关；
    - `tests/test_release_identity.py::TestCanonicalMessage::test_digest_normalization`：全量运行期间（15:40）他人正在新建 `evoagent/release/identity.py` / `tests/test_release_identity.py`（均为未跟踪新文件），运行中读到半成品导致偶发；该用例随后单独复跑 `1 passed`。
- Mock / 本地组件 / 真机 / 真实供应商的验证边界：
  - 本轮使用真实 `RuntimeController`（Mock 模型）与真实 SQLite，负载守护用「数据库中的主任务」计数而非真实宿主机 CPU/内存压力；未接生产调度器、未做多机时钟校准、未做真实多进程并发压测（两进程语义用两个 Database 连接模拟）。
  - 本卡未把调度器接入 `App` 启动/CLI/API（`app.py`、`cli.py`、`api/` 均不在本卡写入边界），接入属集成角色后续动作。

## 回滚与未完成事项

- 回滚触发、操作及验证：`git revert 78a70f15ce5b18dd7c6e0accd4151023ae60833c`（`goal_schedules` 表可保留：幂等建表、无迁移、无既有行为依赖）。回滚后重跑 `tests/test_goal_scheduler.py`（整体消失）与 goal 域回归。
- 保留的限制、后续 ALN 和依赖：
  - 负载守护只看「未完成主任务数」，未纳入真实资源占用（CPU/内存/设备资源），ALN-15-D「设备资源准入与核心任务优先」仍是真正的资源准入卡；
  - `tick` 内 `advance` 是同步 `wait=True`，单目标可能耗时数秒，未做单轮总时长上限（用 `max_goals_per_tick` 近似约束）；
  - 未做「同组件多 Goal 的公平性/饥饿」策略，仅按 `next_due_at` 顺序；
  - 后台线程 `start()` 的进程内生命周期未接入 App（需集成角色按共享文件排期）；
  - 生产阈值（interval/backoff/负载上限）需在真实负载环境标定，Mock 计数不构成生产验收。
- 影响的主题/实体/ADR/契约/系统验收条目：新增 `goal_schedules` 表与 `GoalScheduler`；Goal 十态与授权语义不变；ALN-13 整包关闭仍需前端消费与生产自动推进验证（原包范围）。

## 审阅结论：待审

## 整包结论：仍实现中（ALN-13 整包：前端与生产环境周期推进仍待验；本卡只交周期调度与守护语义）
