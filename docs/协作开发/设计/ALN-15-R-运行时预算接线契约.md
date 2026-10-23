# ALN-15-R：运行时预算接线契约 v1

本契约扩展[15-A账户服务](ALN-15-预算预留与对账契约.md)，供实际Task调用消费。15-A的独立验收只覆盖其原提交；本轮新增行为须另行复核。

## 账户与调用入口

`RuntimeController._make_budget`在分类前绑定持久账户。服务端读取已登记Task、Goal授权及祖先链；客户端的aggregate_budget、candidate_id等manifest字段不构成账户选择权。Goal原budget_ref继续表示任务硬限额，不被改写。

每次实际模型或工具调用，一次SQLite写事务同时占用Task账户、全部Goal祖先、宿主当前日池，以及存在时的可信实验池。共同祖先只占用一次，任何一项不足则全部回滚。金额为USD整数微单位，调用次数为整数；不隐式换汇。provider/expert的旧检查与用量报表继续保留，但尚未转为这些原子账户，不能声称所有预算池都已原子化。

| 入口 | 预留及发送边界 | 实际费用来源 |
|---|---|---|
| IntentClassifier._model_classify | model_call；显式hint不调用模型也不预留 | 本次模型usage |
| PlanningEngine._model_plan | model_call；预算/租约错误传播，不启发式绕过 | 本次模型usage |
| ReactEngine.run | model_call；发送前持久checkpoint与dispatch | 本次模型usage |
| ToolRegistry.execute | 每次实际重试分别预留；执行线程final_guard后领取dispatch | 显式计费回执，或成功的固定单次价格契约 |
| ControllerTaskRunner | 可信experiment_scope覆盖submit_task，消费上述相同入口 | Task实际用量及未知成本标志 |

Task/Goal/实验是生命周期额度，恢复和重试不会清空已花金额。跨午夜的新动作使用当前宿主日池，旧预留的周期与结算归属保持原样；账户镜像仍按预留日期入账，不按结算日期改写。

## 价格与未知费用

已知付费模型按任务及父预算的max_cost_per_model_call保守预留；可信适配器可声明更小的reservation_max_cost。该值是调用上界策略，不是实际账单。真实费用超过上界仍完整落账并阻断后续动作。

未知模型价格默认拒绝发送。宿主可显式配置正数unknown_model_reservation_cost允许一次保守占用；仍无法确定真实成本时转unknown并停止后续付费动作，不把估算当账单。网络工具同理使用明确cost_per_call或正数unknown_tool_reservation_cost。配置均为USD金额，不接受负数、bool、NaN/Inf或零未知价格上界。

模型实际费用只接受本次新鲜usage中的provider/free费用或真实token用量结合已知价格计算。估计费用不用于解除持久占用。明确免费模型也必须预留并消耗调用次数；无供应商计费的本地工具可预留零金额，CPU/磁盘/电量没有被折算为免费资源。

工具回执`evidence.billing={known:true,currency:"USD",cost,receipt_ref}`优先于本地免费声明。工具动作成功但账单未知时，保留真实动作成功/副作用回执，另标cost_reconciliation_required及unknown_tool_costs；不将“费用未知”伪装成“动作未执行”。费用报表及在线监控保留未知工具成本标记，不以已知金额小计代表完整成本。

## 授权、恢复与对账

预留是占额，不是执行授权。发送前重验运行租约、Task终态、Goal期限/撤销、动态收窄额度及Action Guard；dispatch与这些权威检查共享SQLite写事务。取消、撤销或降额后的未发送占用退款。

控制器启动、账户绑定及每次新动作前自动扫描过期预留并记录budget_reservations_recovered事件；已发送未知费用不自动退款。没有独立空闲周期定时器。旧run未领取占用回收；已领取付费占用转unknown，明确免费占用仅结算次数。恢复不重新发送旧动作。

未知费用须通过15-A内部reconcile接口携带真实账单/回执证据处理。运行时没有新增对账HTTP接口或UI。动态授权可收窄既有额度，重新授权不会抹掉已花用量；提高不可变账户额度需要独立显式迁移，不能靠重启或再授权补满。

## 迁移与兼容

- 新增budget_runtime_bindings与budget_runtime_pool_baselines域表；不修改旧schema版本或已发布迁移。
- 首次建立宿主日池时，在同一写事务内扣除旧runtime日用量基线。旧Task历史用量以固定快照幂等导入Task账户，不再向旧runtime池重复记账。
- 发现未迁移的未知历史费用时拒绝创建新日池；有已消费历史的旧Goal拒绝自动建立全额聚合账户，要求显式迁移对账。此处是安全拒绝策略，尚未提供批量旧Goal迁移工具。
- 新聚合账户镜像使用reservation命名空间；旧runtime/provider等报表保留原调用链一次记账。父子/多根账户是同一消费的不同视图，不能求和作为总费用。
- Windows/SQLite应由宿主先初始化新数据库，再启动竞争进程；本轮双进程测试使用该部署顺序，未宣称空文件首连并发初始化已修复。
- 回退前停止新动作、清点已发送/unknown预留并保留审计；删除表或绕过持久账户会丢失约束，不能用作无损回退。

## 验收与未完成范围

专项通过真实App/Controller入口覆盖分类、规划、ReAct、工具、两个Task同Goal及真实双进程竞争、父Goal总额、跨日、未知价格/缺usage/超时、撤销/降额、旧run恢复、旧账迁移防重、实验池、费用与物理回执分别归因。固定提交的完整回归和日志由交付记录提供。

本轮没有真实供应商价格/账单核验；没有CPU/内存/磁盘/电量调度、前后台优先级、生产规模负载或对账界面。账户余额仍按历史流水聚合，长历史性能有待生产测量和汇总设计。独立的非Task嵌入/诊断等模型入口不在本卡四条执行消费链范围内。ALN-14持续执行规则与真实收益证据、ALN-15整包及发布基线不因此关闭。
