# ALN-13-A：Goal十态、持久授权与Task接线

## 行为变化

新增真正的Goal注册表，以09卷十态为准，持久保存目标、归属、父引用、成功条件、期限、授权/预算/停止规则引用、证据与版本。Goal与Task分别维护状态；一次Task成功不会自动宣布长期Goal完成。Goal可显式提出、就绪、运行、等待、暂停、阻塞、复盘、完成、取消或过期。

授权采用五级：suggestion、preparation、sandbox、execution、continuous。建议不创建Task；准备只允许无宿主副作用工具；沙箱走现有SandboxRunner并记录开始/结果摘要；执行与长期推进入口分别派发一次Task，不自动循环。授权只能由admin API签发，调用方不能伪造actor、状态或引用。

父子授权限制工具、权限、文件根、对象、设备、风险、等级、期限和任务预算；子目标不可扩大范围。每次模型调用和工具动作前重新核验Goal及全部祖先的状态、当前授权版本和期限；交互确认后再核验。替换授权、撤销、暂停、取消或过期立即阻止新动作，不声称可撤回已发出的外部操作。

Goal任务、授权绑定和command_id在同一SQLite事务内落库，重复请求只返回原task_id；若进程在落库后、启动worker前中断，可显式resume CREATED任务，运行时沿用现有Run/CAS/租约。Task重试保留原自主动作、预算及任务策略与当前授权的交集，不能绕过后加的限制。

## 接线与兼容

- 新增 `goals/store.py`、`api/goals_api.py`；`core/schema/goals.py`通过现有域发现创建goals、goal_grants、goal_tasks、goal_evidence、goal_commands五表，不改共享persistence.py。
- App暴露goals；controller绑定新任务/重试并注入预算权限回调；AuthorizationStore复用同一策略验证器，将Goal范围接入最终动作检查。
- GET `/goals`、`/goals/{id}`、`/goals/{id}/tasks`、`/goals/{id}/evidence`；POST注册、授权、状态转移、撤销、任务派发、沙箱入口。写操作要求admin，状态写要求expected_revision。
- 新建Goal任务必须引用已注册并获授权的Goal。旧Task JSON仍可读，原来未注册的占位goal_id必须补注册/绑定才能继续执行；scheduled/derived/opportunity不能省略Goal。普通用户直接提交、不带goal_id的任务保持原语义。
- budget_ref对应服务端生成的逐任务硬预算快照，用户只可收窄，专家generous预算仍被Goal上限约束；并非跨任务总额预留。完整语义见[契约](../设计/ALN-13-Goal与自主授权契约.md)。

## 验证

- 源码提交：`af4b655f7842c06701eb71792facbfd1d5483ff6`；最终命令命名空间修复提交：`c885cbdc6464e8d253e9dba840a43aa48d746521`。
- 新增 `tests/test_goal_governance.py` **53项通过**：十态/终态、证据、创建及转移幂等、跨连接CAS、父子九类扩权拒绝、五级动作矩阵、真实沙箱、父授权失效/暂停/取消/替换后的重启拒绝、模型/工具边界、交互确认期间撤销、任务请求去重、创建中断恢复、重试不扩权、真实计算Task和API鉴权/重复派发。
- 相关联合专项曾为 **172 passed / 1 skipped**；此后又补3项直接持久化自主Task绕过入口的拒绝测试及1项命令命名空间碰撞回归，最终53项Goal专项重新通过。
- 全量首轮 **1154 passed / 1 skipped / 2 failed**，两项失败均为IMPLEMENTATION计数尚未同步（⬜15→12），已修正；保留历史日志，不用它冒充最终结果。
- 最终精确提交源码全量 **1162 passed / 1 skipped**（c885cbd），验证见 `.planning/2026-10-22-aln13/evidence.json` 和 `backend-committed.txt`；本轮未改客户端代码，也未以历史客户端或真机结果替代本卡验证。

## 边界与交接

1. 本卡代码交付进入review，需独立reviewer复核，不由作者自批，不发布共同基线。ALN-00/01/04前置的正式独立验收仍保留。
2. 无进展阈值/循环停止、机会排序、自动递归深度/数量治理和自主定时推进归ALN-14；本卡不产生自动后台循环，不声称已覆盖系统验收用例19。
3. 跨任务/目标总预算预留、退款、未知成本对账、设备资源调度归ALN-15；本卡budget_ref与逐任务硬上限可供其消费。
4. 当前是后端/API交付；Goal管理Android界面、多设备长期运行、生产目标数据和真实供应商压力场景另验。沙箱继承现有受控宿主限制，不能当作强恶意代码安全边界。
5. 回退可恢复本次新增域模块和接线至父提交；新增表保留原数据。回退后不得继续将Goal驱动的自主Task作为普通用户Task执行，需暂停其调度。既有未知副作用仍按回执对账，不能以回退代码证明未发生。
