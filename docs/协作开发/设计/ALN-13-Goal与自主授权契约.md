# ALN-13 Goal与自主授权契约

以正式设计09卷§7的十态和五种goal_type为准；05卷七态散文作为历史简述。Goal是长期目的，Task是一次尝试。ALN-13提供有界、显式派发入口，持续调度/机会排序/无进展阈值归ALN-14，跨任务资源预留和总额对账归ALN-15。

## 数据与转移

Goal包含09卷全部字段，增加title、owner、revision、created_at、updated_at。goal_id及parent_goal_id创建后不可更改。成功条件为非空字符串数组；deadline为有限Unix秒，提案可暂缺，授权/推进时必须存在有效期限。派生Goal必须有已注册父目标，子期限不得越过父期限。

| 当前状态 | 允许的后继 |
|---|---|
| proposed | ready、blocked、cancelled、expired |
| ready | running、paused、blocked、cancelled、expired |
| running | waiting、paused、blocked、review、cancelled、expired |
| waiting | ready、paused、blocked、cancelled、expired |
| paused | ready、blocked、cancelled、expired |
| blocked | ready、paused、cancelled、expired |
| review | ready、completed、paused、blocked、cancelled、expired |
| completed / cancelled / expired | 无；终态不能重新授权或复活 |

进入ready/running/review/completed须有效授权、预算快照、期限与停止规则引用；子目标同时核验父链。completed只从review进入，需管理员显式附验收证据引用，Task成功不会自动宣布Goal完成。Goal任务绑定会将ready转running。期限到达后新动作立即拒绝，get/list/expire_due将过期状态与事件持久化。

状态命令要求expected_revision，SQLite BEGIN IMMEDIATE与版本检查保证跨连接只有一个竞争写成功。创建和状态转移可带command_id；相同键/同载荷返回第一次结果，同键改载荷返回409。状态改变及其事件在同一事务中落库，事件保留actor/reason/版本快照。

## 授权矩阵

| level | 可用动作 |
|---|---|
| suggestion | 保留建议，不创建Task、不执行代码 |
| preparation | prepare：可创建只读Task，工具side_effect_level必须为none |
| sandbox | 上述动作及受控SandboxRunner；宿主Task不能用sandbox标签绕过隔离 |
| execution | 上述动作及execute：授权范围内单次Task |
| continuous | 上述动作及advance：可显式派发长期推进的一次Task；不自动循环 |

授权只能由admin API显式签发，actor取传输鉴权结果，不能从body伪造。policy复用任务授权的工具/权限/路径/对象/设备/风险边界，未声明资源维度沿用任务网关的语义。子授权须保留父级已有边界，工具、权限、资源根、风险、等级、期限与预算均不可扩大；执行时再与全部祖先及任务范围取交集。

authorization_ref与budget_ref由服务端生成，绑定不可变授权版本。预算快照使用BudgetLimits的有限非负数值字段，拒绝未知/嵌套覆盖；Task请求只可收窄。模型与工具调用前重新校验目标状态、所有祖先授权版本与期限，并收紧实际预算；专家generous预算也不能越过Goal上限。budget_ref标识本卡逐任务上限，不表示已有跨任务总池预留。

授权替换使旧Task快照失效；撤销、暂停、取消、过期以及父目标停止都阻止新的模型和工具动作。交互确认之后工具网关再次检查，已发送的外部操作仍须按副作用回执对账，不声称能撤回。重试继承原prepare/execute/advance等级、预算与任务策略交集，不能利用新Task恢复旧授权已撤销的权限。

## Task与恢复

Goal任务与授权绑定在同一事务中保存；runtime_manifest.goal保留goal_id、父链授权引用、budget_ref及动作。客户端传入的manifest不构成授权依据。新提交引用未知Goal会拒绝；旧库占位goal_id可查询，但未经绑定不能继续执行。用户直接提交且无goal_id的普通Task保持原语义；scheduled/derived/opportunity新任务必须有有效Goal。

`POST /goals/{id}/tasks`可带command_id，相同请求重放只返回原task_id，不再启动worker；同键改任务内容/动作/策略返回409。若进程在落库后、启动worker前中断，显式任务resume可以继续该CREATED任务，运行时仍使用已有Run/CAS/租约，且持久任务策略不会丢失。RUNNING中断仍走ALN-04接管，不因Goal存在而盲目重复外部效果。

沙箱入口使用既有SandboxRunner的临时目录、网络/进程/凭证限制，超时受目标预算与宿主30秒上限约束；开始/结束记录独立审计版本和摘要。它是受控执行宿主，不宣称可对抗恶意解释器逃逸，也不等于生产任务授权。

## API

GET `/goals`、`/goals/{id}`、`/goals/{id}/tasks`、`/goals/{id}/evidence`可由已鉴权operator/admin查询。POST `/goals`、`/goals/{id}/authorization`、`/transition`、`/revoke`、`/tasks`、`/sandbox`要求admin。创建不接受客户端提供status/authorization_ref/budget_ref。

400用于非法字段，403用于权限/自主等级/授权失效，404用于未知引用，409用于非法转移/版本竞争/幂等冲突。授权与撤销重发使用expected_revision：旧版本重放返回409且无重复效果。只有command_id明确支持的命令返回首次成功结果，不伪称所有POST均可任意重试。
