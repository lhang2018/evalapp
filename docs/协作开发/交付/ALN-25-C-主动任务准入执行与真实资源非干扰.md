# ALN-25-C 主动任务准入执行与真实资源非干扰

任务：ALN-25-C / 主动任务准入执行与真实资源非干扰（P1，阶段3，runtime 线；ZG-08）
负责人：dsh-lead（25-B/30-A 接受后解锁）
审阅人：待定（未分配独立审阅人，不自批）
基线 commit：`a3e05d30748e15d29cd40bb85e8d7a7a2d3afe63`（认领时 HEAD；基线 published `21899e6`）
最终 commit：`4c2a942ee9720bb16a02c10a082b8ab7c100a2f1`（tree `eb7743f267af26dcc05fc00c00b3749689bb3f00`）
认领状态：claimed → **review**

## 触发场景与行为变化

- 触发：25-B 只有"是否允许主动"的策略层；25-A 的只读分析入口没有接到**实际主动任务启动**，也没有原子预算预留与失败释放钩子——可能出现"策略拒绝但仍启动/预留泄漏/未知成本按 0 放行"。
- 修改后（复用 25-B/15-D/30-A，不建第二套执行权威）：
  1. 新增 [goals/proactive_execution.py](evoagent/goals/proactive_execution.py) `ProactiveExecutionService.attempt()`：
     - **无高价值机会**（`opportunity_value<=0`）→ 零启动、零预留；
     - `governor.may_start(concurrent_proactive)` 拒绝（off/资源高压/无进展停止/预算池）→ 零启动，返回理由与安全提示；
     - **未知成本不按 0 处理**：`estimated_cost_known=False` 直接拒绝；
     - 配置预留对象时：`reserve → mark_dispatched → submit`；`dispatch/submit` 失败按失败路径 `refund`，未启动即释放；无提交入口时释放；`reservation_failed`（预算不足/冲突）拒绝启动；
     - 提交入口由宿主注入（既有 controller 通道），本模块不新建执行权威。
  2. [app.py](evoagent/app.py)：装配 `app.proactivity`（`ProactivityGovernor`）与 `app.proactive_execution`；提交走既有 `controller.submit_task(origin="opportunity", goal_id=已授权Goal, principal="local:host", wait=True)`，`actor/reason/opportunity_id/idempotency_key` 写入 manifest_extras；资源决策器由宿主注入（未注入时无压力门，但服务语义完整）。
- 结果：主动任务真正以 `origin=opportunity` 进入既有执行通道；拒绝路径不改写核心任务、不产生外部动作。

## 范围

- 独占模块：`evoagent/goals/proactive_execution.py`（新增）、`tests/test_proactive_execution.py`（新增 12 项）。
- 共享文件补丁（精确点）：`evoagent/app.py`（+23 行装配/submit 方法）。
- 未改：`goals/proactivity.py`、`core/controller.py`（复用既有通道）、预算/身份权威。
- 兼容：纯新增；无 schema/迁移。

## 验证证据

- 专项：`pytest tests/test_proactive_execution.py -q` → **12 passed**：无高价值零启动、off 拒绝+安全提示且零提交、资源高压 defer 零提交、未知成本拒绝、成功 reserve→dispatch→submit、submit 失败 refund、reserve/dispatch 失败零启动/释放、无提交入口释放、非法参数拒绝、**App 真实提交 origin=opportunity 且绑定授权 Goal**、关闭档位零新增、**真实宿主 CPU 压力下主动让出且核心任务 RUNNING 状态不变**。
- 相关回归：`test_proactive_execution + test_proactivity + test_resource_admission + test_goal_scheduler` → **43 passed（7.26s）**。
- 实测证据：`.planning/2026-10-26-aln25c/proactive-execution-evidence.json`，**8/8 通过**（真实 App/controller/授权 Goal/宿主 CPU 压力）。
- 全量后端：`pytest tests/ -q` → **2508 passed / 0 failed / 1 skipped（412.88s）**，日志 `.planning/2026-10-26-aln25c/backend-tests.txt`。
- 边界（如实登记）：
  - **预留路径用可注入预留对象验证**；App 未装配真实 `BudgetReservations` 账户（15-D 账户创建属运维初始化），因此默认 App 路径不预留；
  - 资源决策器默认未注入（15-D 需要宿主 sampler/阈值），App 默认无压力门；
  - 模型为本地 Mock，未做真实 LLM 核心任务质量对照；服务端策略下发/客户端 UI 未接；
  - 真实设备/生产证据未采。

## 回滚与未完成事项

- 回滚：`git revert 4c2a942ee9720bb16a02c10a082b8ab7c100a2f1`（删服务/测试与 app 装配；无 schema）。
- 保留缺口：
  1. App 装配真实 15-D 资源决策器与预算预留账户（需配置/运维初始化）；
  2. 服务端策略下发与客户端档位 UI；
  3. 真实 LLM 核心任务质量/延迟对照与生产压力证据；
  4. 主动机会→高价值评分链路（25-D/opportunity outcome）未接。
- 影响：ZG-08 的"策略→实际启动/预留/停止"闭环服务与 App 入口交付。

## 审阅结论：待审

## 整包结论：仍实现中（真实决策器/预算账户、策略下发与UI、真实质量对照待补）
