# EvoAgent Android 客户端（React Native）

> PRD 89 用户端功能的最小可运行骨架：任务提交、任务列表轮询、状态与结果展示、任务取消。

## 为什么选 React Native

- UI 层 100% JS/TS —— 满足「后续迭代可以直接推送」的要求：
  - **OTA 路线 A（推荐）**：接入 `react-native-code-push` 或 `expo-updates`，
    发布只推送 JS bundle，不重新上架/安装。
  - **OTA 路线 B**：`App` 可整体换成 WebView + 远端 React Web bundle（`evoagent/api/server.py` 直接作为其后端）。
- 原生层只保留壳（MainActivity + Manifest + 权限），原生能力（无障碍/Intent）
  在后续迭代按 PRD 68/69 通过 DeviceAdapter 接入，权限全部走系统授权。

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
├── android/                   # 原生壳
└── package.json
```

## 未包含（后续迭代）

- 无障碍服务 + 手机操作（PRD 68）
- 语音输入（PRD 64）
- 记忆中心 / 预算与模型设置 / 更新管理页（PRD 89.4-89.8）
- 后台任务通知（PRD 64）
