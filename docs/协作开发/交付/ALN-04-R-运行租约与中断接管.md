# ALN-04-R：运行租约与中断接管交付

负责人 codex-runtime；独立审阅人待分配。基线 HEAD 为 `23d395076e54aa0721f9fdb2fec940ca21b5ebe1`，实现位于其上的共享候选工作区。用户明确要求认领并开发，候选尚未发布不再阻止本轮增量接线；不据此宣称 ALN-00-B 已发布或整个 ALN-04 已验收。

## 行为变化

之前只有领取 Run 的原子比较，没有进程所有权和持久心跳。RUNNING 的执行进程被杀后，普通 resume 只返回旧状态。本轮新增显式管理员接管：有可核验的过期所有权、恢复检查点且无未决副作用时，原子结束旧 Run 为 INTERRUPTED，建立 parent_run_id 指向它的新 Run，沿用任务、版本检查、历史输出和预算。活跃租约拒绝接管。

管理员入口：`POST /tasks/{task_id}/recover`；本机入口：`python -m evoagent.cli recover <task_id>`。API 新调用返回 202 仅表示调度受理，继续查询任务及 `/tasks/{task_id}/runs`；不存在为 404，不满足接管条件为 409，未认证/非管理员按既有鉴权返回 401/403。没有自动扫描接管所有历史任务。

## 所有权协议

| 项目 | 当前实现 |
|---|---|
| 身份 | 每个 RunManager 的 worker UUID；每次执行独立 run_id |
| 租约 | SQLite run_leases 表，以 task_id 为主键，记录 run/owner/generation/expiry/active |
| 领取 | BEGIN IMMEDIATE，与 Task CAS、旧 Run 收尾、新 Run 建立同一事务 |
| 续租 | 默认 TTL 15 秒，约每 TTL/3 续租；仅相同 owner/run/generation 且尚未过期可续 |
| 代次 | 接管或下一次正常执行递增，释放后保留 generation，不回到 1 |
| 隔离 | ContextVar 传播到工具超时线程和工作流线程；执行边界校验，关键仓储写入在事务内校验 |
| 释放 | 仅同 owner/generation 可以释放；旧执行者不能关闭新代次；未完成旧 Run 保留过期记录供接管 |
| 失败 | 续租失败后不续命；后续执行与写入失败关闭；已发出的外部请求仍需账本/供应商对账 |

新增 execution_budget_state 表保存任务及工作流累计用量；模型与实际工具尝试在调用前预记，费用返回后只结算费用，不重复计次。工作流每批开始前也持久化预算/输出。恢复优先读取较新的累计用量，不能因“账本已 confirmed、检查点尚未更新”而清零调用次数。预记后、实际发送前崩溃可能保守多计一次。

ReAct 模型调用中断会保留 model_call_inflight 标记；新任务状态标记 cost_reconciliation_required。这个标记说明旧请求的账单尚需核对，不表示费用为零，也没有替代 ALN-08 的真实供应商账单整合。

## 故障矩阵与本地证据

| 故障点 | 恢复行为 | 验证 |
|---|---|---|
| 模型调用阻塞后 kill 进程 | 建子 Run，旧调用次数保留，标记费用待核对 | spawn 真实进程、共享 SQLite、Process.kill |
| 批准已消费、网关尚未预记账 | 不复用已消费批准；原动作重新进入待审批，不执行 runner | 真实 kill，旧批准仍为 used、新 pending ID、外部计数为零 |
| planned 后、submitted 前 | 原 Run 收尾，任务 SUSPENDED，planned 转 unknown；不创建投机子 Run | 真实 kill，无 runner 调用 |
| 已提交/外部成功、回执未写 | 任务等待对账；force 也不能绕过网关重放 | 真实 kill，外部计数始终为 1 |
| confirmed 且回执已缓存、检查点未写 | 网关复用原结果，补齐工作流输出，任务/作用域预算保留 | 真实 kill，外部计数 1、两级 tool_calls 均为 1 |
| Task/最终检查点已保存、Run 尚未结束 | 只收尾旧 Run，保留终态，无新子 Run | 真实 kill，Run 数量 1 |
| 两个独立进程竞争同一过期租约 | 只有一个领取成功 | spawn 两进程竞争 |
| 旧 worker 仍活着但心跳失效，接管后模型返回 | 禁止覆盖新结果；调用者返回最新持久 Task | 两 App、真实阻塞线程、过期后接管 |
| 等待用户/已取消/终态的失联收尾 | 保留等待问题或原终态，不重跑 | 仓储/控制器测试 |
| 缺累计预算检查点或旧任务没有租约 | 明确拒绝或隔离为 SUSPENDED，不推测可以执行 | 仓储/控制器测试 |

专项命令：`python -m pytest tests/test_process_recovery.py tests/test_run_ownership.py -q`。共 15 项（7 项进程故障/竞争 + 8 项所有权/入口测试）。共享工作区全量 **764 passed / 1 skipped**，日志见 `.planning/2026-10-22-design-alignment/claimed-backend-final.txt`；台账检查见 claimed-checks.txt。测试使用本地 Mock 模型与文件计数工具，未宣称验证真实供应商扣费。

## 共享接线与兼容

- 独立模块：core/leases.py、schema/leases.py、tests/test_run_ownership.py、tests/test_process_recovery.py。
- 运行线接线：runs.py、controller.py、react/engine.py、workflows/engine.py、tools/registry.py、budget.py。
- 集成线审阅补丁：persistence.py 的关键写入 fencing、预算快照仓储；task_control_api.py、auth.py、cli.py 的受控恢复入口。沿用原 approvals、effects 与 Task 权威。
- 新表通过已有域 schema 自动发现，以 CREATE TABLE IF NOT EXISTS 添加；没有改动旧表列/删除记录或冒用新的 schema_version。没有所有权记录的历史 RUNNING 任务可读，但不能自动视为进程已死。

## 限制与后续

持久租约过期、进程死亡、外部操作未发生是三个不同事实。使用同机墙钟判断过期；时钟前跳会提前使旧 owner 失效，后跳会延后接管，因此没有跨机器时钟一致性承诺。SQLite/磁盘失败在执行前的预算或账本写入边界阻止新调用；没有进行真实断电或磁盘故障注入。任意自定义 fn 自行启动的额外线程不自动继承 ContextVar，外部动作必须走现有工具网关。

自动执行接管限定为有完整检查点的 RUNNING/SUSPENDED；分类/规划/验收中间态不自动推测如何续跑。无检查点的隔离任务保留 recovery_blocked，不能 force 绕开。等待审批的失联 Run 仍走原审批机，审批详情和 TTL 保留。

回滚时停止领取新任务，保留 SQLite 新表和所有账本；不要删除 generation、未知副作用或预算记录后重试。升级后的失联运行应先在此版本完成收尾/对账，再回退执行代码。独立审阅、ALN-04 指定检查点/节点取消和真机组合验收仍待完成。

审阅结论：待审；子卡状态 review，整包保持实现中。

## 提交候选复验

源代码与本交付记录同次提交。父提交为 `a957fe8`；提交候选副本后端全量 766 passed / 1 skipped，唯一环境失败为副本缺少 Git 元数据；补齐元数据后该项单独复验通过，合计 767 项通过。候选客户端 136 passed/typecheck 通过，未混入他人在途通知实现。源码与验证日志随提交保留；不把这次提交等同于共同基线发布或独立验收。
