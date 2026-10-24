# ALN-11-W 交付：活动 workflow 执行消费与健康探针

2026-10-24最新验收：**按用户授权由codex-runtime统一复验通过既定切片（accepted）**，自己的修复不伪称第三方，见[统一验收](../验收/2026-10-24-全部待审核统一验收.md)。下文保留原交付时点及边界。

任务：ALN-11-W 活动workflow执行消费与健康探针
负责人：zcode-main（2026-10-24 接手 codex-runtime 排队现场开工）
审阅人：待定（须独立复核，不接受自审自收）
基线 commit：`f464881`（实际接手源码锚点；基线仍 candidate，`published_commit` 为空）
分支与最终 commit：main，代码 `352e8ef`；台账随本交付登记提交（见 git log "docs: ALN-11-W 交付登记"）
认领状态：delivered（review，待独立复核）

## 执行消费协议（契约）

提供方：已验签装载的活动能力包（发布端产物，服务端仅下发字节，不解析内容）。
消费方：客户端 `BundleRuntime`（任务路径与健康探针共用）。

- 输入：`execute(input: Record<string, unknown>, task_ref: string)`；
  探针输入取包内 `health_probe.input`，缺省 `{}`。
- 包扩展字段（增量，全部可选，旧包不受影响）：
  - `workflow_def`：服务端 `WorkflowDef.to_dict()` 的可序列化子集
    （`workflow_id/version/input_schema/output_schema/nodes[]`，节点仅
    `tool` 引用，无 `fn/condition`）；`workflow_def.workflow_id` 必须与
    包标识 `workflow` 一致。**不引入另一套工作流格式。**
  - `intent`：声明消费的客户端意图（当前如 `time_query`）。
  - `min_app_version` / `required_permissions`：兼容声明，执行前校验。
  - `health_probe`：`{input, expect_output_contains?, expect_output_equals?}`。
- 状态与记录：每次执行产生 `BundleExecutionRecord`
  （`execution_id/release_id/version/digest/workflow_id/workflow_version/
  task_ref/steps[]/outcome: succeeded|failed|timeout`）；探针产生
  `BundleProbeRecord`（`outcome: healthy|unhealthy|unknown`）；恢复产生
  `BundleRecoveryRecord`。全部持久化在 KV `bundle_runtime_v1`
  （records/probes 各保留 20 条），重启后版本关联可查。
- 幂等键：`execution_id/probe_id` 由客户端生成；重复探针追加记录、
  不覆盖历史绑定；管理器回执仍走既有 `apply_id` 幂等重放。
- 授权范围：工具节点只能引用客户端注册工具；设备动作由 App 注入的
  `open_app` 工具实现走既有 `DeviceBroker` 确认/风险/副作用边界；未注册
  工具、权限不足、不兼容、非法 DAG 在**执行前**拒绝
  （`BundleRejectedError`，code：`no_active_bundle/not_executable/
  incompatible/tool_unregistered/permission_missing/illegal_workflow`）。
- 健康判定：healthy ⇔ 真实执行成功且期望相符；执行前拒绝与执行失败
  （含期望不符）= unhealthy；超时/无结果 = unknown。unknown 既不晋级
  `last_known_good` 也不自动回滚；unhealthy 交由既有
  `UpdateManager.healthCheck(false)` 自动回滚，回滚后 `verifyRecovery`
  用同一探针输入真实执行原健康包并落恢复证据。
- 故障与重启语义：执行超时有硬上界（默认 10s）；记录先落 KV 再返回；
  进程重启后从 KV 重建，不依赖内存态。
- 下载内容不 eval：节点参数为纯数据，工具实现由 App 注册。

## 触发场景与行为变化

- 输入/触发：真实制品激活后自动探针；用户提交活动包声明意图的任务。
- 修改前：App 只读取展示 `activeBundle`；健康通过/失败全为手工调用；
  切槽成功即视为更新完成，新能力是否真的可执行无证据。
- 修改后：内嵌 `workflow_def` 的活动包在任务路径被真实执行（同一输入
  在更新前后产生可区分输出，证据绑定 release/version/digest 与任务引用）；
  激活后自动运行有界探针驱动健康判定与回滚恢复。

## 范围

- 独占模块（新增）：`android-app/src/update/bundleRuntime.ts`；
  `android-app/test/update/bundleRuntime.test.ts`。
- 共享文件补丁：`android-app/App.tsx`（BundleRuntime 构造与工具注册表、
  submit 执行前校验+执行消费、激活后自动探针、更新面板运行时区）；
  `android-app/src/update/workflowBundle.ts`（装载时校验 `workflow_def`
  结构与标识一致性，迁移入 `validateWorkflowDef`）。
- 未改动：`manager.ts`（探针复用既有 `healthCheck/rollback` 回滚接口，
  无需修改）；服务端代码零改动；通知/后台 runner/DevicePackage 在途
  改动原样保留未触碰。
- 数据迁移：无。旧格式包（无 `workflow_def`）保持可装载、仅展示不可
  执行（`not_executable`），不破坏既有 11-A 装载语义。

## 验证证据

- 命令：`cd android-app && npm run test:offline` → **190 passed / 0 failed**
  （原 174 + 本卡 16）；`npm run typecheck` → 通过（tsc 5.9.3）。
  源码 commit 见文首；`git diff --check` 干净。
- 正常路径（新增测试）：同一输入更新前后两版本 workflow 输出可区分
  （`旧版输出` vs `新版输出`），执行记录分别绑定
  `rel_old@1.0.0` / `rel_new@2.0.0` 各自 digest 与 `task_ref`；
  DAG 按依赖拓扑执行并传递上游输出。
- 执行前拒绝：无活动包、旧包不可执行、未注册工具、不兼容
  （`min_app_version`）、权限不足、非法 DAG（环/重复ID/未知依赖/标识
  不一致）六类全部拒绝且工具不被调用；装载层对非法 `workflow_def`
  拒绝装载且不改变活动包指向。
- 探针：healthy（期望相符）/ unhealthy（期望不符、执行失败、已激活
  非法包）/ unknown（超时）三态齐备；unknown 不标健康、不触发回滚。
- 故障回归（真实制品链路）：RSA 非对称签名 + 内存文件系统走完整
  `check→prepare→activate`，坏版本探针 unhealthy →
  `healthCheck(false)` → `rolled_back`（回执含 `rel_old` 与
  `to_release_id`）→ 活动包指回 `rel_old` → `verifyRecovery` 真实执行
  原健康包成功；新实例（同 KV）重启后 unhealthy/恢复记录与版本关联
  完整保留，重复探针追加不覆盖。
- 验证边界：以上全部为 Node 自动化（内存存储、本地 RSA、无真实网络）。
  **真机证据（验收第 5 条：健康失败→真实回滚→旧能力可执行）尚未采集**，
  自动化通过与真机待验分开登记。

## 回滚与未完成事项

- 回滚触发与操作：本卡不改变更新管理器既有回滚语义；探针 unhealthy
  走既有 `healthCheck(false)` 自动回滚，无需新增回滚路径。
- 保留的限制与后续：
  1. 真机故障证据待补（模拟器/真机安装含坏 workflow 的签名包 →
     观察自动探针 → 回滚 → 旧包可执行）；
  2. 服务端下发侧的 `workflow_def` 打包工具与真实发布流程未做
     （发布端仍只负责字节下发；打包属发布线范围）；
  3. ALN-11 整包关闭仍按原范围：生产信任根、多设备灰度、11-R 独立复核
     与真机验证另验。
- 影响主题：O-07（版本分发、激活与回滚）、R-06/R-07（工作流运行时与
  执行模式）、E-03（诊断与执行解耦——探针只判定与取证，不在生产路径
  自动修复）；无 ADR 变更，无 schema 迁移。

## 审阅结论

待审（review）。独立复核建议重点：执行前拒绝覆盖面、unknown 不标健康
的判定路径、回滚后活动包指向与恢复证据的版本绑定。
整包结论：仍实现中。
