# EvoAgent Android 客户端（React Native）

> PRD 64–76 / 89：任务中心、离线优先队列、无障碍节点操作与风险闸门。

## 已实现

- **任务中心**：提交/取消/对账，状态、结果、失败原因展示；在线状态条与手动同步、队列统计。
- **离线优先**（`src/offline/`）：任务与事件写入同一本地快照；联网后事件批量幂等上传，
  `LOCAL_ONLY` 任务以稳定 `client_request_id` 补交；连续 checkpoint、死信、指数退避；
  隐私过滤（sensitive 不上传，secret/private 拒绝入队）。
- **设备操作**（`src/device/` + `android/` Kotlin）：无障碍服务按文本/描述点击节点、
  拉起显式 Intent；低/中/高风险分级，高风险需独立确认；执行结果五态并要求证据；
  已下发未验证的动作不自动重放。
- **无坐标/手势注入**：`canPerformGestures=false`，不绕过系统权限。

## 为什么选 React Native

- UI 层 100% JS/TS —— 满足「后续迭代可以直接推送」的要求：
  - **OTA 路线 A（推荐）**：接入 `react-native-code-push` 或 `expo-updates`，
    发布只推送 JS bundle，不重新上架/安装。
  - **OTA 路线 B**：`App` 可整体换成 WebView + 远端 React Web bundle（`evoagent/api/server.py` 直接作为其后端）。
- 原生层只保留壳 + 无障碍服务 + `DeviceModule`，所有能力均由系统授权后使用。

## 测试（无需 node_modules）

```bash
npm run test:offline    # node --test：离线队列/同步/任务状态/设备闸门，共 27 项
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
- 真机调试改 `App.tsx` 中的 `baseUrl` 为局域网 IP；发布版本建议走 `usesCleartextTraffic=false` + HTTPS。

## ⚠️ 构建验证状态

**本仓库开发环境没有 Android SDK、模拟器或 `node_modules`，Gradle 构建、无障碍服务与真机
行为均未实际验证。** 原生壳已按 RN 0.75.4 标准模板对齐，仍需在真机/模拟器上确认。

手动构建：

```bash
cd android
./gradlew assembleDebug        # 或：npx react-native run-android
```

真机验收清单：

1. `adb shell settings get secure enabled_accessibility_services` 能看到 `com.evoagent/.DeviceAccessibilityService`。
2. 未授权时客户端显示「服务未启用」，点击「点击页面文本」返回 `unavailable`，不崩溃。
3. 授权后 `isServiceEnabled()` 为 true；在设置页点击已知文本返回 `verified`。
4. 高风险动作（发送/支付/删除）弹出独立系统确认框；取消后状态为 `denied`，无侧效应。
5. 飞行模式下提交任务得到 `LOCAL_ONLY`；恢复网络后自动转 `SUBMITTING → SUBMITTED`，
   服务端只产生一个任务（同一 `client_request_id`）。
6. 同步中杀进程再启动：任务显示 `NEEDS_RECONCILE`，确认重提后不产生重复任务。
7. 服务端重复接收同一 `event_id` 返回 `duplicates`，客户端 checkpoint 正常推进。

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
├── src/storage/               # AsyncStorage ↔ KeyValueStore 适配
├── test/                      # node --test（offline 19 + device 8）
├── android/                   # Kotlin 原生壳 + AccessibilityService + DeviceModule
└── package.json
```

## 未包含（后续迭代）

- 视觉/OCR 操作、坐标手势（明确不做自动注入）
- 语音输入（PRD 64）
- 后台常驻同步（当前仅前台轮询 + 手动同步）
- 记忆中心 / 预算与模型设置 / 更新管理页（PRD 89.4-89.8）
- 后台任务通知（PRD 64）
