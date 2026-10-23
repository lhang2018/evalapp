# 发现

- 无 .codegraph，遵照 AGENTS 跳过。
- dfcfcee 新增 trace.py、planner RecoveryPolicy、export execute_deletion/export_to_file。三卡明确尚未接运行时/API。
- 当前工作区已有 ALN-14-A 独立审阅报告、Android 通知及历史日志在途，全部保留。
- 台账固定时点：39张，accepted24、review10、claimed4、blocked1。ALN-14新报告初查时未提交，随后0c501bf由第三方正式登记接受，已保留。
- 独立探针19项在修复前全失败：16-X真实LayerManager返回嵌套diagnostics字典导致删除后TypeError；scope/校验开关绕过；空事件跳过证据；无持久墓碑；导出覆盖/硬链接覆盖；07-T全链依赖/循环/重复ID/快照别名；07-R非法动作与参数别名。
- 三卡原有测试未改；首轮修复+相关回归147项通过。16-X语义明确为整个task日志/证据删除，事件ID只是快照引用，不冒充精确事件删除授权。
- 最终7组问题、27探针；修复前固定源码26失败/1通过；相关155、固定全量1521/1。新增JSON快照深冻结，稳定拓扑序；真实删除与导出审计均有重开SQLite验证。
