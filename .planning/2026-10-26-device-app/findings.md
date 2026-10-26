# 发现

- App已有DeviceBroker/NativeDeviceTransport实例与显式高风险确认；没有TaskActionLedger/classifyPipeline挂载。
- LocalTaskService.flush处理LOCAL_ONLY，任务自动刷新读取有server_task_id的待处理记录。本机动作必须有显式execution_target并排除服务端提交，避免重复执行。
- 本机提议在确认前需持久化，但不能预先claim TaskActionLedger，否则执行器会视为崩溃未决。准备提议保存在本地任务；真正执行仍由既有executeProposal原子claim。
- 任务成功需要用户确认成果，动作回执仅显示动作状态。最小回执事件复用离线队列；不上传目标文本、证据正文或动作参数。
- 取消只允许未claim的prepared阶段；claimed或未知结果只能读取回执，不自动重发。台账写入后任务投影/队列失败可从原回执恢复，不再调用执行器。
- 原生桥和React测试渲染器没有增加外部依赖；集成验证复用真实TS仓库、协议、broker、NativeDeviceTransport与SyncEngine，原生模块/事件服务边界使用替身，App JSX以类型检查核对。
- 新增21项边界测试中19通过/2失败，确认两个实际问题：OfflineRepository仅实例内串行导致服务重建/后台实例可丢写；回执入队布尔标记阻止未知→核验后的新状态上传。改为同一KV对象+存储键共享串行队列，以及最小回执投影签名去重；不声称跨进程锁。
