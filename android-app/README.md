# EvoAgent Android 客户端（React Native）

> PRD 64–76 / 89：任务中心、离线优先队列、无障碍节点操作与风险闸门。

## 已实现

- **任务中心**：提交/取消/对账，状态、结果、失败原因展示；在线状态条与手动同步、队列统计。
- **离线优先**（`src/offline/`）：任务与事件写入同一本地快照；联网后事件批量幂等上传，
  `LOCAL_ONLY` 任务以稳定 `client_request_id` 补交；连续 checkpoint、死信、指数退避；
  隐私过滤（sensitive 不上传，secret/private 拒绝入队）。
- **可靠同步**：每事件带 `run_id` + run 内 `sequence_no`、`runtime_manifest_id` 与
  `clock_source=device`；显式 `pending → in_flight → acknowledged` 状态机，进程重启后回收
  stale in-flight 批次；稳定批次号与 `protocol_version` 回执，只删除服务端 `confirmed`
  事件；永久拒绝进入隔离区保留证据，可在界面人工重新入队。
- **容量治理**：critical / normal / debug 分级保留（7 天 / 1 天），80% 预警；空间不足时
  优先清理低等级事件并写入损失标记，界面提示，不静默删除未同步证据。
- **设备操作**（`src/device/` + `android/` Kotlin）：无障碍服务按文本/描述点击节点
  （精确匹配优先、子串仅作回退）、拉起显式 Intent、读取当前窗口节点快照；点击/启动返回
  操作前后快照，由 JS 判定页面变化或前台包名；权限弹窗识别；低/中/高风险分级，高风险需
  独立确认；执行结果五态并要求证据；已下发未验证的动作不自动重放；连续 3 次未验证安全停止，
  需人工恢复。
- **无坐标/手势注入**：`canPerformGestures=false`，不绕过系统权限；快照仅回传前台 JS，
  不落盘、不上传。
- **版本对账与更新管理器**（`src/update/`）：安装实例 ID 持久化；`/sync/reconcile` 对账
  （平台/最低版本/工具契约/灰度）；本地复核签名、摘要、交付方式与回滚路径；双槽位原子切换、
  健康检查失败自动回滚、应用回执上报。原生 APK 只标记为需二进制更新，必须走平台应用商店。
- **候选阶段同步**：`/sync/candidates` 拉取评测后的候选状态（批准/就绪/部署/监测/晋级/回滚），
  在更新分区只读展示；候选阶段不触发激活，实际更新仍走对账与签名校验。

## 为什么选 React Native

- UI 层 100% JS/TS —— 满足「后续迭代可以直接推送」的要求：
  - **OTA 路线 A（推荐）**：接入 `react-native-code-push` 或 `expo-updates`，
    发布只推送 JS bundle，不重新上架/安装。
  - **OTA 路线 B**：`App` 可整体换成 WebView + 远端 React Web bundle（`evoagent/api/server.py` 直接作为其后端）。
- 原生层只保留壳 + 无障碍服务 + `DeviceModule`，所有能力均由系统授权后使用。

## 测试（无需 node_modules）

```bash
npm run test:offline    # node --test：离线队列/同步/任务状态/设备闸门/界面观测/更新管理器，共 69 项
npm run typecheck       # 需要先 npm install（tsc）
```

Node 24+ 原生 type-stripping 直接运行 `.ts` 测试；源码内跨模块导入带 `.ts` 扩展名以满足
Node 解析，Metro 同样可解析。

## 构建与运行

```bash
# 1) 启动服务端（本机）
cd ../ && python -m evoagent.cli serve --port 8300

# 2) 安装 JS 依赖
cd android-app && npm install

# 3) Metro 起包 + 编译安装到设备/模拟器
npm run android
```

- 模拟器默认连 `http://10.0.2.2:8300`（宿主机）。
- 真机可直接用 `adb reverse tcp:8300 tcp:8300` 后在 App 顶部把服务端地址设为
  `http://127.0.0.1:8300`；地址会持久化到 AsyncStorage，重启后保留。
- 发布版本建议走 `usesCleartextTraffic=false` + HTTPS。
- 已配置 `metro.config.js` 与 `babel.config.js`；`app/build.gradle` 的 `react {}` 中调用
  `autolinkLibrariesWithApp()`（AsyncStorage 等原生包依赖 autolinking）。

## 构建验证状态

**已验证**（2026-10-20）：`./gradlew assembleDebug` 构建通过；在模拟器（API 37）与真机
（HyperOS，Android 16 / API 36）安装运行；在线任务、真实模型回复、离线队列与事件同步、
无障碍节点点击与启动（返回 `[verified]` 且带前后快照/页面包名证据）、无页面变化降级为
`[unverified]`、连续未验证安全停止与人工恢复均实测通过。

手动构建：

```bash
cd android
./gradlew assembleDebug        # 或：npx react-native run-android
```

验收清单（已在真机走通 1–4 与 7；5–6 由单元测试覆盖）：

1. `adb shell settings get secure enabled_accessibility_services` 能看到 `com.evoagent/.DeviceAccessibilityService`。
2. 未授权时客户端显示「服务未启用」，点击「点击页面文本」返回 `unavailable`，不崩溃。
3. 授权后 `isServiceEnabled()` 为 true；在设置页点击已知文本返回 `verified`。
4. 高风险动作（发送/支付/删除）弹出独立系统确认框；取消后状态为 `denied`，无侧效应。
5. 飞行模式下提交任务得到 `LOCAL_ONLY`；恢复网络后自动转 `SUBMITTING → SUBMITTED`，
   服务端只产生一个任务（同一 `client_request_id`）。
6. 同步中杀进程再启动：任务显示 `NEEDS_RECONCILE`，确认重提后不产生重复任务。
7. 服务端重复接收同一 `event_id` 返回 `duplicates`，客户端 checkpoint 正常推进。
8. 更新分区「检查更新」返回候选后依次执行校验/激活/健康检查；点击「健康检查失败」后
   自动回到上一版本并上报 `rolled_back`（回执含 `to_release_id`）。

## OTA（直接推送升级）接入步骤

1. `npm i react-native-code-push`（或 `npx expo install expo-updates`）。
2. `MainActivity.kt` 继承带 CodePush 的 ReactActivity；Application 注册 `CodePush.build(...)`。
3. `App.tsx` 外层包 `codePush(...)` HOC。
4. 发布：`npx appcenter codepush release-react -a <owner>/EvoAgent-Android -d Production`。
   JS 层所有界面改动即可「直接推送」。

## 目录

```
android-app/
├── App.tsx                    # 任务中心 UI（PRD 89.1/89.2）
├── index.js                   # RN 入口
├── src/api.ts                 # REST 客户端（对应 api/server.py）
├── src/offline/               # 本地任务/事件队列、隐私过滤、同步引擎
├── src/device/                # 设备操作契约、风险闸门、原生桥接
├── src/update/                # 安装实例、版本对账、候选阶段、双槽位更新管理
├── src/storage/               # AsyncStorage ↔ KeyValueStore 适配
├── test/                      # node --test（offline 21 + device 20 + update 19）
├── android/                   # Kotlin 原生壳 + AccessibilityService + DeviceModule
└── package.json
```

## 未包含（后续迭代）

- 视觉/OCR 操作、坐标手势（明确不做自动注入）
- 语音输入（PRD 64）
- 后台常驻同步（当前仅前台轮询 + 手动同步）
- 能力包制品下载与装载（当前更新管理器只切换槽位引用，实际包内容交由 OTA 层）
- 候选阶段推送（当前为拉取式同步，无服务端主动推送）
- 后台任务通知（PRD 64）
