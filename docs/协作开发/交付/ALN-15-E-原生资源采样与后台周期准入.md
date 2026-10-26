# ALN-15-E 原生资源采样与后台周期准入（本地组件切片）

任务：ALN-15-E / 原生资源采样与后台周期准入（P1，阶段3，Android 线；ZG-04/ZG-08）
负责人：dsh-lead（25-B 一审接受后解锁）
审阅人：待定（未分配独立审阅人，不自批）
基线 commit：`efe5ac0eccf1538eef086f94ce3c93452349e720`（认领时 HEAD；基线 published `21899e6`）
首轮 commit：`cefdc7c61148a736572992d8779d7b3a15f95067`（tree `ecc6fa2e6b5d9448f247fd2ef346bc8770ed8320`）
认领状态：claimed → **review（本地组件切片；真机压力实测未做，不冒称通过）**

## 接续复核：当前实现以 6e82856 为准

codex-runtime 复核确认：首轮 Kotlin 的 valid 只检查内存，TS 也允许缺CPU的样本据内存放行，因此“首次CPU无基线不冒称低压”尚未成立。6e82856 已在两层要求 CPU 与内存同时有效，CPU无基线/不可读均如实不可用；同时拒绝未来采样时间和非法当前时钟，导出判定也不再把 NaN 压力当低压。

固定 6e82856 客户端 **459 passed**、TypeScript 检查通过，新增12项负向/边界探针全部通过。**本轮没有重跑 Kotlin 编译、安装 APK 或真机压力验收**；首轮447项/编译日志仍只作历史证据，以下为首轮历史描述。详见[复核报告](../验收/2026-10-26-主动执行与原生资源采样复核.md)。

**接受仍阻塞**：index.js 的真实 HeadlessTask 仍直接注册 runStoredBackgroundSync，没有注入 resourceSampler/highWater；注册原生模块和可注入 runner 不等于后台已消费采样。须接既有策略下发、真实原生 sampler（处理首次基线及 /proc/stat 不可读）、补真实后台拒绝/恢复/前台不受影响证据。本轮不另造客户端默认阈值。新补丁待另一审阅人复核，原 owner/reviewer 保留。

## 触发场景与行为变化

- 触发：后台周期工作需要有**带来源/单位/采样时间/不可用状态**的原生 CPU/内存/电量/磁盘采样，并接到既有资源准入，避免在高压/采样失效时仍跑后台；不得复制预算/阈值权威。
- 修改前：Android 端无资源采样模块；`backgroundRunner` 后台周期不查询任何资源状态；无"采样缺失/过期不伪造低压力"语义。
- 修改后（复用既有 15-D 决策语义，不内置阈值权威）：
  1. 新增 [ResourceSamplerModule.kt](android-app/android/app/src/main/java/com/evoagent/ResourceSamplerModule.kt)：读取 `/proc/stat` CPU（需两次采样计算差值，首次无基线不伪造低压力）、`ActivityManager.MemoryInfo` 内存、`BatteryManager` 电量、`StatFs` 磁盘；返回 fractions、`units`、`source`、`sampledAtMs`、`valid`、`errors`；采样失败 `valid=false`；已在 `DevicePackage` 注册。
  2. 新增 [src/device/resourceSampler.ts](android-app/src/device/resourceSampler.ts)：`parseResourceSample` 校验分数有限且在 [0,1]、采样时间与时效（`stale_sample`/`missing_*`/`invalid_*` 一律 `valid=false`，**绝不伪造低压**）；`samplePressure` 取 CPU/内存较大值；`backgroundResourceDecision` 的 `highWater` 必须由调用方从既有策略传入（缺失/非法即拒绝，不内置默认阈值）。
  3. [backgroundRunner.ts](android-app/src/offline/backgroundRunner.ts)：后台周期前可选注入 `resourceSampler`；高压/不可用/陈旧/采样异常 → 返回 `background_deferred:*` 且**不执行后台动作**；未注入时行为完全不变（兼容既有测试）。
- 阈值权威：不在客户端复制预算/阈值，`highWater` 由既有策略传入；前台任务不受该 helper 影响。

## 范围

- 独占/边界内文件：`android-app/android/app/src/main/java/com/evoagent/ResourceSamplerModule.kt`（新增）、`DevicePackage.kt`（+1 行注册）、`src/device/resourceSampler.ts`（新增）、`src/offline/backgroundRunner.ts`（可选注入）、`test/device/resourceSampler.test.ts`（新增 7 项）。
- 未改：后端 Python、`App.tsx`、其他设备模块；未新增第二套预算/身份/执行权威。

## 验证证据

- 新增适配测试：`node --test test/device/resourceSampler.test.ts` → **7 passed**（合法采样保留来源/单位/时间；缺失/非法/过期不伪造；压力取较大值；无阈值拒绝；高压拒绝后台、低压放行）。
- 客户端全量：`npm run test:offline` → **447 passed / 0 failed**；`npx tsc --noEmit` → **0 错误**。
- 原生编译：`gradlew :app:compileDebugKotlin --offline` → **exit 0**（仅三方依赖 deprecation 警告）。
- 探针证据：`.planning/2026-10-26-aln15e/client-evidence.json`，**10/10 通过**（含首次证据静态探针误报 false 已更正，文件内 `correction` 字段记录）。
- **未做的真机验收（如实登记）**：
  - 未重建/安装新 APK，未在设备（`5748d823` 已连接）上运行原生采样与后台周期；
  - 未做真实资源压力测量下的 F04-03（后台让位）与 F04-04（关闭即停）端到端；
  - 无原生单元测试（Kotlin 仅编译验证）；本切片不冒称真机/生产通过。

## 回滚与未完成事项

- 回滚：`git revert cefdc7c61148a736572992d8779d7b3a15f95067`（删模块/适配/测试并还原 runner 注册；无 schema/迁移）。
- 保留缺口：
  1. **真机端到端与压力实测**（安装 APK → 采样 → 后台准入 → 高压/关闭/恢复）；
  2. 原生 Kotlin 单元测试与设备 API 差异（部分 OEM 电量/StatFs 行为）未覆盖；
  3. 既有服务端 15-D 阈值/策略下发到客户端的链路未接（本轮由调用方注入 highWater）；
  4. 后台执行结果/采样不可用状态的上报与用户提示未接 UI。
- 影响：ZG-04 的客户端采样与后台准入组件交付；真机与策略下发属后续。

## 审阅结论：待审（本地组件切片，不替代真机验收）

## 整包结论：仍实现中（真机压力实测、Kotlin 单测、策略下发/UI 待补）
