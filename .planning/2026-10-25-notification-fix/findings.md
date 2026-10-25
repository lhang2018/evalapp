# 已确认缺陷

- Kotlin notifyTaskUpdate明确返回boolean，关闭通知或系统拒绝返回false；TS接口声明unknown且无条件delivered++，把false/空回执记成功。
- 全局通知开关打开、单独任务channel被关闭时，原生代码仍调用notify并返回true；需要检查已有channel.importance，不覆盖用户设置。
- LocalTask保留server_status，FAILED/CANCELLED均映射DONE；通知仅以failure_reason判失败，故没有原因的FAILED和CANCELLED被称为“任务完成”。权威服务端终态应优先于残留文本。
- 通知所需的前后tasks.list及notifier创建处在后台同步最外层try；同步已成功后若通知读盘/适配器构造抛错，会被改成ran=false、online=false。需把通知附加过程与实际同步结果隔离。
- 前台启用后台同步已显式请求POST_NOTIFICATIONS；不新增权限绕过或静默授权。通知为尽力而为，未承诺跨进程恰好一次、离线补投或用户已阅读。
- 真实后台组装探针还发现sync.flush返回{ok:false,error}但不抛异常，调用层原先忽略失败、继续刷新和通知，掩盖真实上传错误。增加本组装层的明确失败传播，既有SyncEngine重试与持久化权威保持不变。
