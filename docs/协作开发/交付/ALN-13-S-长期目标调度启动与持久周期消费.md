# ALN-13-S 长期目标调度启动与持久周期消费

> 2026-10-26 接续审查：修正stop超时线程丢失/重复启动，running与stopping如实显示；周期与超时有限值校验。生产长跑、多进程和资源治理联调仍待。 本轮固定相关358项通过，修复提交0ba37e1/01766f5，新增修复待其他审阅人复核。下文修改前后及首轮验证是历史记录，最新行为以[复核报告](../验收/2026-10-26-新增宿主与音视频费用复核.md)为准；不提升整卡或生产接受。

任务：ALN-13-S / 长期目标调度启动与持久周期消费（阶段3，runtime 线）
负责人：dsh-lead（本会话认领；按"按优先级顺序认领"接 P1 READY 卡）
审阅人：待定（未分配独立审阅人，不自批）
基线 commit：`89a88d6aca9245fbcd5f16054ae9991da443844d`（认领时 HEAD；基线 published `21899e6`）
最终 commit：`0bf8e05809c744825f7b05ddcb5b6db10848b3af`（tree `0b0a080d181bc3cb6dbd60652b2573778fd2d20d`）
认领状态：claimed → **review**

## 触发场景与行为变化

- 触发：长期目标需要**按持久到期时间周期推进**，并且宿主要有启停、可观测（周期/延迟/失败/恢复）入口；重启或多实例不得重复派发。
- 修改前：ALN-13-P 已交付 `GoalScheduler`（持久 `goal_schedules`、租约+fencing、主任务负载守护、失败退避、`tick/due/stats/start/stop`）与 `GoalGovernor.advance`；但**没有任何宿主接线**——App 未装配 scheduler，CLI/API 无启停与观测入口。
- 修改后（全部复用 13-P/13-A，不重写调度/预算/身份）：
  1. `scheduler.py`：`tick` 记录 `ticks/last_tick_at/last_report`（含 `primary_load_guard` 提前返回路径）；新增 `status()`（running/owner/started_at/ticks/last_tick_at/stats/load/policy/last_report）；`start` 记录 started_at，`stop` 清理；
  2. `app.py`：装配 `app.goal_scheduler = GoalScheduler(goal_governor, db, bus, owner_id="host_goal_scheduler")`；
  3. `cli.py`：新增 `goals-scheduler-status`、`goals-tick --now`、`goals-scheduler-start --poll-seconds`（Ctrl+C 停止并打印最终状态，不遗留失控循环）；
  4. `api/goals_api.py`：新增 `GET /goals/scheduler/status`、`POST /goals/scheduler/tick|start|stop`（写操作 admin；status 可由 operator 读取）。

## 范围

- 独占模块：无新增模块；扩展 `evoagent/goals/scheduler.py`（卡片写入边界内）。
- 共享文件补丁（精确修改点）：`evoagent/app.py`（+5 行装配）、`evoagent/cli.py`（3 个命令与解析/分发）、`evoagent/api/goals_api.py`（文件末尾追加 4 个端点）。未改 `GoalGovernor`/`GoalStore`/预算/身份实现。
- 新增/修改契约：scheduler `status()` 观测结构、CLI 三个命令、API 四个端点；无 schema 变更（`goal_schedules` 沿用 13-P）。

## 验证证据（首轮历史记录）

- 专项：`python -m pytest tests/test_goal_scheduler_host.py -q` → **6 passed**：
  - App 装配 scheduler 且 `status()` 字段完整（running=false/ticks=0/stats/load/policy）；
  - 真实到期推进恰好一次并生成任务；`status().last_report.tick_id` 与 tick 报告一致；
  - **重启同槽位不重复派发**：同库新 App 实例再 tick 无新 advance，任务数仍 1；人为把到期时间拨回同槽位重放，`command_id` 幂等仍只有 1 个任务；
  - 门控拒绝（`PermissionError`，如预算不足）进入 `report.stopped` 且 `status().last_report` 可见；
  - API：operator 可读 status；admin tick 返回报告、operator tick 403；start→running=true，stop→running=false；
  - CLI：`goals-tick` 输出报告、`goals-scheduler-status` 输出 ticks/last_report。
- 相关域回归：调度/治理（test_goal_scheduler_host + test_goal_scheduler + test_goal_governance）→ **69 passed（16.07s）**。
- 实测证据：`.planning/2026-10-26-aln13s/scheduler-host-evidence.json`，**9 项检查全过**（真实持久化重启、同槽位重放幂等、任务计数、start/stop 时延 <6s）。
- 全量后端：`python -m pytest tests/ -q` → **2368 passed / 0 failed / 2 skipped（386.67s）**，日志 `.planning/2026-10-26-aln13s/backend-tests.txt`。
- 边界：
  - start/stop 在**进程内**验证（同进程线程启停），未做长跑生产 daemon 与系统重启后的自动拉起（宿主退出可停止，无遗留线程已断言）；
  - 多实例并发为**顺序两实例 + 同槽位重放**验证（依赖 13-P 租约/fencing 与 `command_id` 幂等），未做多进程真并发压测；
  - 资源让出当前由 13-P `primary_load_guard`（主任务在途）覆盖；15-D ResourceGovernor 压力让位**未在本卡重复接线**（避免重复权威，按整包另验）；
  - 未接真机/生产环境；无长跑时的周期抖动/漂移统计面板。

## 回滚与未完成事项

- 回滚：`git revert 0bf8e05809c744825f7b05ddcb5b6db10848b3af`（撤回 status/started_at 记录、App 装配、CLI 命令与 API 端点；`goal_schedules` 表沿用，无迁移）。
- 保留缺口：
  1. 生产宿主长跑/系统重启自动拉起（systemd/容器 entrypoint）未接；
  2. 多进程真并发压测与资源压力实测未做；
  3. 15-D ResourceGovernor 压力让位与主动性档位的端到端联调属整包范围；
  4. App/客户端观测入口未接（CLI/API 已可观测）。
- 影响：ZG-01/ZG-08 的宿主启停与持久周期消费交付；长期目标整包仍按父包跟踪。

## 审阅结论：待审

## 整包结论：仍实现中（宿主长跑/多进程压测/资源压力联调待补）
