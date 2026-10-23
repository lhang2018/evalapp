# ALN-15-A：持久预算账户、预留与费用对账

作者：codex-runtime；状态：待独立审阅，不关闭ALN-15整包，不发布基线。

功能源码提交：`8c479b6c9f01f342920e2f52a19d96a0e1700364`。测试清理修复：`6976662b035af5d7d114f8e6e2bc160b91dc826a`。契约见[预算预留与对账v1](../设计/ALN-15-预算预留与对账契约.md)。

## 已交付行为

- `BudgetLedger(database).reservation_store()` 提供内部持久接口：create_account、reserve、mark_dispatched、settle、refund、mark_unknown、reconcile、recover_expired、balance/history。
- Goal/Task/实验/池账户具有不可变身份、父引用、币种与生命周期硬额度；子账户按同维度同币种收窄，预留同时占用每层祖先，防止兄弟任务分别花掉同一总额。
- 金额使用整数微币种单位，调用量/token使用整数。禁止省略费用当零，不做换汇；计费动作须先给正数保守费用预留。
- SQLite BEGIN IMMEDIATE把检查、预留、领取、审计与终态记账放在原子事务中；复用Database锁及运行租约fence。支持多线程、多连接、多进程竞争，不依赖“先查再写”。
- 预留幂等键绑定账户、额度和租期；终态重放不重新占额。dispatch仅领取一次；领取前再次核对祖先unknown和实际超支，余额预留不等于权限授权。
- 未领取可退款，已领取超时/进程异常退出则unknown并保留占用，阻断关联额度上的新动作；即使恢复扫描尚未执行，已领取过期记录也会阻断。unknown只能以完整实际用量及证据引用显式对账，不按过期自动退款。
- 实际费用超出预留/上限仍如实结算，保留overrun和负余额，后续调用停止。结算和budget_ledger账户用量镜像同事务，重复结算不重复记账；存储失败回滚预留状态、用量与审计。

## 代码与兼容

- 新增 `evoagent/core/reservations.py`、`evoagent/core/schema/reservations.py`、`tests/test_budget_reservations.py`。
- `evoagent/core/budget.py` 仅增加opt-in入口，不更改原BudgetController硬预算或旧record/check语义。
- 新账户/预留/关联/事件表通过域schema自动发现；不修改旧表、不改SCHEMA_VERSION和历史迁移。
- 旧日/月用量镜像使用 `reservation:<currency>:<account_id>` namespace，日期绑定预留发生时点。父子scope是同一支出的不同视图，不相加；tokens保存在新账本，权威余额不使用旧浮点报表值。

## 验证

- 新卡39项，联合预算、计量、Goal与schema回归 **150 passed**，日志[regression.txt](../../../.planning/2026-10-23-aln15/regression.txt)。
- 覆盖同连接多线程争抢、真实双进程同库争抢、并发结算一次入账、三级引用总额、幂等/冲突、重启、运行租约失效、完整输入/非法数值、真实超支和未知成本。
- 子进程在dispatch标记提交后直接 `os._exit(23)`；父进程恢复验证仍有持久占用并转unknown，没有自动退款或重复下发。此处是本地账本进程崩溃验证，不是供应商端计费故障注入。
- 原代码新增入口验收记录为3个setup errors（缺reservation_store）；它只证明接口缺失，不宣称3个业务漏洞反例。另有[旧check/record竞争探针](../../../.planning/2026-10-23-aln15/legacy-race.txt)：1次额度可同时通过2次检查，说明旧接口不能替代新原子预留；旧接口行为本卡有意不改。
- `8c479b6` 固定源码首轮全量 **1218 passed / 1 skipped / 2 warnings**。两条警告为既有同步API测试在异步执行线程结束前关闭测试数据库；已用 `6976662` 修正测试清理等待，不改生产运行时/吞异常。[首轮证据](../../../.planning/2026-10-23-aln15/committed-8c479b6.json)保留。相关61项以线程异常警告升级为错误复验通过。
- 最终固定源码 `6976662` 全量 **1218 passed / 1 skipped**，线程异常警告升级为错误后通过，无首轮线程警告。客户端Git tree与8c相同，沿用其 **166 passed/TypeScript通过**，没有重复跑无变更客户端。元数据、声明与台账检查均通过；精确commit/tree、命令、日志和SHA256见[committed-evidence.json](../../../.planning/2026-10-23-aln15/committed-evidence.json)。不使用工作区在途内容作为提交证据；日志hash按UTF-8 LF规范计算。

## 接线与剩余范围

**这是可由后续消费者调用的账本服务，现有Controller、模型/工具、Goal创建入口尚未自动接入。** 不因独立账户测试通过宣布运行时已按聚合额度预留。

1. 后续运行时消费者须在Action Guard及13的授权/期限检查内，绑定真实Goal/Task/实验到权威预算账户，执行reserve→一次性dispatch→settle/unknown；当前reference是内部可信调用者的追踪引用，不代替对真实实体的鉴权。
2. 13原budget_ref仍为不可变任务硬限额引用，未被偷换为聚合账户。新账户的真实实体绑定及旧费用流与新预留结算的去重接线须单独交付，不能同时重复计费。
3. 下一张为[ALN-15-R运行时接线](../任务卡/15-预算预留运行时接线.md)。14可先消费契约准备规则/夹具，持续执行仍需运行时接线；真实收益归因证据依赖09/10。
4. 真实供应商价格/账单核验、CPU/内存/磁盘/电量、前台后台优先级、API/界面和生产规模性能测试仍未包含。余额查询目前按账户历史预留聚合，长期大量流水时需用测量决定汇总/归档方案。

## 回滚与独立审阅

停用本卡消费者或回退入口即可恢复旧路径；新表是增量表，先保留账户/unknown记录及审计，不能为回滚删除尚未对账的占用。未来有运行时消费者后，先停止发新动作并清点预留再回退。

独立reviewer需复核父链原子性、重复请求、dispatch边界、unknown及真实超支、崩溃恢复和旧账本镜像去重。作者不自审；基线保持candidate。
