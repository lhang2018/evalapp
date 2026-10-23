# ALN-15-A 预算预留与对账契约 v1

本卡提供 `BudgetLedger(database).reservation_store()` 内部服务。使用同一 Database 连接、锁与既有预算用量表，新增账户/预留/引用/事件域表，不修改已发布迁移。金额是指定币种的整数微单位（1元=1,000,000 cost_micros），不使用浮点余额，不隐式换汇。

## 权威与引用

- 账户绑定 `kind=goal/task/experiment/pool`、`reference`、`parent_id`、币种和硬额度；创建后不可修改，同账户相同参数重试返回原记录，换参数拒绝。
- 子账户保留相同维度/币种且每维上限不大于每层父账户。每次预留同时占用全部祖先，兄弟账户不能各自花掉同一父额度；后续结算/退款一起释放或转实际用量。
- 账户是生命周期总额，不因日期切换或重启重置。日/月分配要显式建周期账户，不能拿预留租约当作预算周期。
- 引用是供可信内部消费者追踪的预算身份，不是授权。创建账户、选择父账户及reference与真实Goal/Task/实验身份的绑定由消费方的授权/编排入口负责；本服务不接受来自模型的扩额，也不取代13的授权与期限检查。
- 现有Goal `budget_ref` 仍是任务硬限额引用；本卡不修改它、不虚称自动映射到聚合账户。

## 输入与原子性

资源维度为 `cost_micros/model_calls/tool_calls/tokens`；单位为0～2^53−1整数，拒绝bool、负数、浮点、NaN/Inf、字符串和未知维度。预留和实际用量须显式给齐账户维度，不省略费用当零。计费账户若预留调用量，须给正数保守费用上界；无可用上界则不能发起计费动作。仅需要非金额额度的账户可不设费用维度。

所有写操作要求actor/reason；对账另需非空evidence_ref。账户、检查余额、预留、状态转移、审计、最终用量写入均使用SQLite `BEGIN IMMEDIATE` 事务，复用运行租约fence，支持多连接/多进程竞争。禁止嵌套调用以免提交或回滚调用者的外部事务。

预留的 `(account_id,idempotency_key)` 唯一，同键不同额度/租期拒绝；闭合记录重放只返回旧终态，不重新占额。`mark_dispatched` 是一次性领取，重复领取拒绝，调用方不得把查询或重放预留当作再次执行许可。

## 状态与恢复

| 操作 | 前置状态 | 结果与额度 |
|---|---|---|
| reserve | 全祖先有余额且无待对账债务 | reserved；先持久预留，后执行 |
| mark_dispatched | reserved、未领取、租约未过期，祖先仍无unknown或超支 | 一次性持久领取，再允许调用方经既有Action Guard发请求 |
| refund | reserved且从未领取 | refunded；释放全部占用，重复退款不重复释放 |
| settle | reserved且已领取、实际用量完整且有证据引用 | settled；预留转实际用量，退还差额；实际全零为refunded |
| mark_unknown | reserved且已领取 | unknown；保留保守占用，阻止关联祖先下新的预留/领取 |
| reconcile | unknown且实际用量完整、有账单/回执证据引用 | settled或真实全零的refunded；仅此路径允许解决未知费用 |
| recover_expired | reserved过期 | 未领取→refunded；已领取→unknown；不重发调用，不因过期认定免费 |

即使恢复任务尚未运行，已领取且过期的记录也会阻止新的额度使用。unknown没有自动退款时间；新余额请求及先前已预留但尚未领取的请求都会重新检查祖先。实际费用超过预留/硬上限时如实落账，available可为负；不能为保持“预算绿”丢弃真实支出，后续调用停止并保留overrun。

## 结算与旧账本

结算、审计、原预留终态与旧 `budget_ledger` 的账户用量镜像同事务提交；任一步失败全部回滚。同终态相同actual/evidence的重试只读旧结果，不重复记账，变更终态结算拒绝。

旧账本镜像scope为 `reservation:<币种>:<account_id>`，按预留发生日期记录日/月，金额从整数微单位转换为原有报表浮点字段，权威余额仍用整数。父子scope是不同视图，不应相加；tokens只在新账本保留。原runtime/provider等用量池不重复计入，原record/check与硬限额语义不改；旧record流量并不会自动进入新聚合额度。

## 错误与交付边界

余额/待对账阻断使用 `BudgetExceeded`；幂等/状态冲突使用 `ReservationConflict(ValueError)`；非法输入ValueError，缺引用KeyError，过期运行所有权LeaseLost，存储异常回滚后原样上抛。事件含schema_version、全部预算引用、revision、actor/reason、状态及证据引用，不保存模型正文/供应商密钥。

本卡交付内部账户服务、幂等新表及并发/进程崩溃测试。现有Controller、模型/工具调用、Goal任务创建尚未自动调用本服务；运行时接线、真实价格/账单核验、APRO调度及设备资源属于后续范围。14可消费本契约准备规则，不能据此宣称持续运行已受聚合预留约束。
