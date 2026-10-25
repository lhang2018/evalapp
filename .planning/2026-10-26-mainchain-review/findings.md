# 发现

- 台账 07-C 和 12-E 均为 review；07-C 先前原子回滚与竞争补绑已复跑，12-E 的 a1b3a31 修复只覆盖提议准备与回执投影，未实现 App/broker 端到端挂载。
- 不能把纯组件测试或生成 launch_intent 提议写成真机任务主链完成。本轮复核身份、授权、并发/恢复边界与调用消费，保留真实设备及生产装配缺口。
- .codegraph 不存在，按用户规则不创建索引。
- 实际恢复入口 controller._reconciliation_gate 会先 heal_pending 再三方对账，未知/歧义转等待用户；计划或绑定缺失走显式 legacy skipped 路径。
- 待验证：DeviceBroker 将内部 action 直接传给确认回调，回调可能改目标/身份；安全停止仅在异步等待前检查，等待 availability/确认期间其他动作触发停止后仍可能下发。
- 待验证：确认/原生 ok 使用真值判断，运行时异常字符串可能被当成同意/成功。新增探针使用本地替身，不操作设备。
- 12 项负向探针全部复现：确认回调改写目标/任务/嵌套参数；非布尔真值被当授权；availability/确认等待期间跨越安全停止；原生非布尔 ok 被当 verified；非布尔服务状态被当可用。已通过隔离确认快照、严格 true 判定、下发前复检停止状态修复，相关72项通过。
- ReAct engine 的 record_intent/resolve_intent 与 controller 对账确有生产消费点；本轮只读验证07-C，不扩大为整包接受。
