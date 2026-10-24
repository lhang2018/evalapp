# ALN-12-A 交付：自然语言任务—设备执行/回执协议与离线故障矩阵

任务：ALN-12-A 自然语言任务—设备执行/回执协议与离线故障矩阵
负责人：zcode-main（2026-10-24 按队列认领开工）
审阅人：待定（须独立复核，不接受自审自收）
基线 commit：`c61b53e`（实际接手源码锚点；基线仍 candidate，`published_commit` 为空）
分支与最终 commit：main，本卡代码与台账随本交付提交（见 git log "feat(ALN-12-A)"）
认领状态：delivered（review，待独立复核）

## 触发场景与行为变化

- 输入/触发：任务链路需要把一次自然语言任务映射到一次设备动作并留下
  可对账的证据；任一故障（服务端/模型/网络/同步/进程/资源/权限）发生时
  需要单一权威的降级语义。
- 修改前：`DeviceAction`/`ActionJournalEntry` 没有 task_id/run_id/effect_id
  关联，任务无法追溯到效果；broker 幂等日志仅内存，重启后同 action_id
  会重复下发；无统一的 effect 引用与可上传回执结构；故障降级规则分散
  在各模块，无单一权威矩阵。
- 修改后：`taskProtocol.ts` 提供提议（含 task/run/action/effect 引用、
  风险、预期观察）→ broker 执行 → 回执（终态/证据/时间戳，映射
  `JournalState`）全链路；台账持久化，重启后同一 action_id 仍不重复下发，
  未知副作用（dispatched/unverified）永不自动重放，重试 = 新提议新
  action_id；上传投影只含引用与状态，参数/文本目标/消息/证据内容本地
  保留。`failureMatrix.ts` 收拢 11 类故障的降级/重试/停止/对账/设备动作
  规则与本地保留字段。

## 范围

- 独占模块：`android-app/src/device/taskProtocol.ts`、
  `android-app/src/device/failureMatrix.ts` 及对应测试。
- 共享文件补丁（卡内授权的小改，由并行协调会话完成，与本协议同源对齐）：
  `types.ts` 加可选 task_id/run_id/effect_id（DeviceAction 与
  ActionJournalEntry）；`broker.ts` 导出 `journalStateForStatus` 并把动作
  上的可选引用复制进日志条目（同一 action_id 引用不可变）。两者都不改
  既有风险分级/确认/幂等/安全停止语义。
- 缺口证明夹具：`test/device/taskGaps.test.ts`（协调会话在实现落地前
  编写，证明任务未关联设备动作、broker 内存日志重启重复下发、无统一
  effect 引用三个缺口；实现后作为回归保留，三条当前全绿——缺口1/3
  断言"裸 broker 层无引用/无全局对象"在协议层引入后依然成立，缺口2
  断言的 broker 原生重复下发行为由协议层台账修复，broker 本身不改）。
- 契约提供方：客户端任务协议层（本卡）；消费方：App 主链路接线
  （后续小卡）、设备闸门（已对齐）、服务端副作用账本（effect_id 语义与
  ALN-02/07-B 对齐：effect_id 是动作的副作用账本身份，一经分配不可变）。
- 数据迁移/兼容：全部新增字段可选，旧动作/旧日志不受影响；台账
  `device_task_ledger_v1` 版本化，坏数据回退空态。
- 本卡不改：`notifications*.ts`、`nativeNotifications.ts`、
  `NotificationModule.kt`、`backgroundRunner.ts`、`DevicePackage.kt`
  （历史在途通知改动，保持原样）；服务端仅提接口需求不接线。

## 验证证据

- 正常路径：提议→执行→verified 回执→任务链（listForTask）可查，
  引用随动作进入 broker 日志（taskProtocol.test.ts 首条断言）。
- 拒绝/失败/重复/重启：高风险无确认 denied、无障碍未授权 unavailable
  （重试需新提议）、failed/unverified 不自动重放、同 action_id 幂等重放
  原回执且 transport.calls 不变、重启（新 ledger 实例）后引用与回执完整、
  台账容量上界淘汰最旧。
- 故障矩阵：11 类全部覆盖，每条六要素（可见状态/降级/重试/停止/对账/
  设备动作规则）非空，全矩阵设备动作规则一致包含"不自动重放"类约束，
  隐私基线（敏感内容不入上传载荷）与投影测试互相印证。
- 命令与结果：`cd android-app && npm run test:offline` → **214 passed /
  0 failed**（原 190 + 本卡 24：taskProtocol 12 + failureMatrix 9 +
  taskGaps 3）。typecheck 由并行会话声明通过，本机无 node_modules 且
  全局 tsc 损坏未能复跑，留独立复核验证。
- Mock/真实边界：全部为 Node 原生 TS 单测（FakeTransport/MemoryStore）；
  真机自然语言执行、断网/杀进程/重启一致性、敏感内容证据按 ALN-12
  整包验收另行采集。

## 回滚与未完成事项

- 回滚：本卡为纯新增 + 可选字段增量，revert 单提交即可回到 190 项
  基线；无数据迁移需要逆转。
- 未完成（按任务卡原范围保留）：App 主链路接线（提交任务→提议→执行）
  按后续小卡推进；服务端 effect 绑定消费（与 ALN-07-B 的
  action_bindings 对齐）另行登记范围；真机证据另验。
- 影响主题：PRD 64-68（设备操作）、64-76（离线优先）、21（安全边界）；
  ALN-12 整包关闭仍需真机一致性证据。

审阅结论：待审
整包结论：仍实现中
