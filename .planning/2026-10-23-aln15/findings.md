# 设计发现

- 现有BudgetLedger.check和record分离，无法原子预留跨任务额度；只有事后日/月用量，不含reservation状态。
- Database使用同连接RLock；持久操作用BEGIN IMMEDIATE及assert_connection_lease，跨连接/进程互斥由SQLite保证，禁止嵌套事务或调用会自行commit的Database方法。
- Goal budget_ref只是任务级不可变硬限额引用；新聚合账户必须单独定义，不覆盖原Goal授权或声称自动绑定。
- 金额使用整数微货币单位，调用量使用非负整数；账户有明确币种，禁止隐式货币折算。
- unknown不按零计价，不因租约到期退款。预留后标记dispatch再调用，未dispatch过期可回收，已dispatch过期转unknown并保留占用。
- unknown/真实超支在旧预留领取前也要重查祖先；已领取过期即便恢复任务尚未运行，也阻断新预留，不能有恢复扫描空窗。
- 所有结算镜像到budget_ledger的reservation:<currency>:<account_id>命名空间；不重复记到runtime/provider池，不改变旧check/record。权威余额用整数，旧报表镜像浮点只作视图。
- 工作区实际新增Claude18-B并在开发中提交；保留其认领/代码，不把其内容当作本卡开发或验证前置。其他提交也已包含部分上轮认领元数据，按实际Git来源记录，不amend。
