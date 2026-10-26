# 发现

HEAD 仍为 e5e3337，上一轮241相关测试证据完整。接续发现潜在遗漏：SUSPENDED 分歧分支直接 WAITING_USER 不在允许边；_move 会先持久化状态并发事件，门禁后续才填 waiting_request/failure_reason，状态事件失败可能留下缺失裁决事实的状态。下一步真实恢复/数据库故障注入确认，不改其他会话内容。

正式16项新探针修前9失败/7通过，确认两类问题；独立数据库连接读持久结果，覆盖两种来源状态、计划/绑定两类故障、state_transition/reconciliation_gate两处事件故障。实际audit.record抛OSError由既有_audit吞掉，保持尽力而为，不算新业务缺陷。初版直接替换_audit令其抛异常绕过实际捕获，初版13失败/1通过日志仅开发探针历史，不作正式缺陷计数；已改正注入位置。

修复将waiting_request/failure_reason在_move保存前填好，删除冗余第二次save；状态与裁决事实共用一次任务写入。新增SUSPENDED→WAITING_USER合法边，不扩展终态出边、授权或工具执行。
