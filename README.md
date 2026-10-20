# EvoAgent — 自进化个人智能体平台（参考实现）

依据两份基线文档实现的可运行工程：

- `EvoAgent自进化智能体产.md` — 产品需求文档（PRD 1.0，28 部分）
- `EvoAgent自进化智能体系.md` — 架构设计文档（Architecture 1.0，23 章）

条目级代码映射见 [IMPLEMENTATION.md](./IMPLEMENTATION.md)。Android 客户端见 [android-app/](./android-app/)。

## 快速开始（无需 LLM Key）

```bash
# 全链路演示：意图→规划→ReAct→工具→验收→记忆
python -m evoagent.cli demo
# 工作流演示（AI 漫剧 DAG）
python -m evoagent.cli workflow
# 测试集
python -m pytest tests/ -q
# 客户端离线/设备核心测试（Node 24 原生 TS，无需 node_modules）
cd android-app && npm run test:offline && cd ..
# REST API（供 Android / 管理端）
python -m evoagent.cli serve --port 8300
# 直接提交任务
python -m evoagent.cli task "计算 2024+2000+500" --intent computation
# 暂停 / 恢复（FAILED 任务会重跑为新任务；未知副作用需 --force）
python -m evoagent.cli pause <task_id>
python -m evoagent.cli resume <task_id> --force
# 诊断：失败任务 → 多假设案例 → 固定探针（无外部网络）
python -m evoagent.cli diagnose <task_id> --probe tool_health
python -m evoagent.cli diagnose            # 最近诊断案例
```

诊断 REST 端点：`GET /diagnostics`、`GET /diagnostics/{case_id}`、
`POST /diagnostics/scan`、`POST /diagnostics/{case_id}/experiments`、
`POST /diagnostics/{case_id}/candidates`。诊断永不自动发布；候选仍走评测门禁。

## 接入真实 LLM（OpenAI 兼容：OpenAI/DeepSeek/Moonshot/vLLM…）

```powershell
$env:EVOAGENT_PROVIDER="openai"
$env:EVOAGENT_API_KEY="sk-..."           # 对应 config.provider
$env:EVOAGENT_BASE_URL="https://api.deepseek.com/v1"   # 可选，自定义兼容端点
$env:EVOAGENT_MODEL="deepseek-chat"
python -m evoagent.cli task "查询现在几点了" --intent time_query
```

Mock 与真实模型共用同一适配器契约；路由/档案/注册表逻辑一致（PRD 4.5 能力与模型解耦）。

## 架构总览（对应 Architecture 2.x 双轨）

```
Android 客户端 (RN/JS) ──REST──▶ ┌─ 稳定运行轨道 ─────────────────────┐
                                 │ CLI/API ─ RuntimeController        │
                                 │   ├─ PlanningEngine                │
                                 │   ├─ ReactEngine（模型建议+控制器执行）│
                                 │   ├─ WorkflowEngine（DAG）          │
                                 │   └─ 验收 Verifier                 │
                                 │ 模型路由 MockModel / OpenAICompat   │
                                 │ 工具注册表（契约/权限/预算/幂等）      │
                                 │ 记忆与上下文（SQLite）               │
                                 └────────────────────────────────────┘
                                 ┌─ 独立进化轨道 ─────────────────────┐
   运行事件 → 诊断案例 → 进化控制器 → 候选实验 → 评测门禁 → ReleaseManager │
   （签名制品 / 灰度 / 回滚）                                              │
                                 └────────────────────────────────────┘
```

## 安全边界（PRD 21 部分）

- 高风险工具（写文件等有副作用）默认 `confirm`：无确认回调即拒绝；`deny` 模式全拒。
- 硬预算由 `BudgetController` 程序化执行，模型无法绕过。
- 更新/发布须通过评测门禁与签名验证；核心安全边界改动禁止普通发布流程。

## 离线优先与设备操作（PRD 64–76）

客户端（`android-app/`）在断网时仍可提交任务并持续记录事件，联网后按幂等键安全重放：

- **本地队列**：任务与事件写入单一快照（AsyncStorage），提交任务与入队事件同一次落盘；
  队列容量 500，满则拒新；`seq` 单调递增，`checkpoint` 只推进连续已确认前缀。
- **幂等同步**：事件以客户端生成的 `event_id` 为主键 `INSERT OR IGNORE`，重复批次返回
  `duplicates`；任务提交带 `client_request_id`，服务端 `client_requests` 表命中即返回原
  `task_id`（`202 deduplicated`）。失败重试与崩溃恢复都不会产生重复任务。
- **ack 后才删**：服务端确认前事件不删除；连续失败进死信并提供手动重试，不静默丢弃。
- **隐私保守**：仅 `standard` 事件上传；`sensitive`（设备动作、位置、记忆写入等）本地保留；
  含密码/令牌/证件号的载荷在入队前直接拒绝。
- **崩溃恢复**：`SUBMITTING` 重启后转 `NEEDS_RECONCILE`，需用户确认后重提；已下发但未验证的
  设备动作禁止自动重放。

设备操作走 Android AccessibilityService 的**节点级**通道（按文本/描述点击、拉起显式 Intent）：

- 低/中/高风险分级；高风险（发送消息、支付、删除等）必须通过独立系统确认框，不依赖模型提示词。
- 结果五态：`verified | unverified | failed | denied | unavailable`——执行后必须有证据才算成功。
- 不提供坐标注入、不绕过系统权限；服务未启用时返回 `unavailable` 并引导用户去系统设置授权。

**未验证部分**：本机无 Android SDK/模拟器，Gradle 构建、无障碍服务与真机行为均未实际运行验证。
手动构建步骤见 `android-app/README.md`，验收清单列于同一文档。

## 已实现 / 迭代占位

完整矩阵见 IMPLEMENTATION.md。当前一轮：服务端核心全链路可运行 + 规则诊断/探针 + 真实模型
Planning/意图分类 + 执行内核接线（计划驱动 ReAct/进度持久化、模型调用硬预算、路由接入运行路径、
暂停/恢复）+ 离线队列/幂等同步 + 无障碍节点操作 + 测试通过（Python 92 项、客户端 27 项）。
迭代占位：视觉/OCR 操作、后台常驻同步、真实生成服务适配、向量检索、Web 管理端、隔离沙箱执行候选代码。

## 目录

```
evoagent/                  Python 服务端核心
  core/                    任务模型/状态机/控制器/预算/权限/检查点/事件/持久化
  planning/  react/        Planning 与 ReAct 引擎
  models/                  Mock + OpenAI 兼容适配器 + 路由 + 模型注册表
  tools/                   工具契约/注册表/内置工具
  memory/                  记忆存储与上下文装配
  experts/                 专家注册表（首批 5 个专家）
  workflows/               工作流 DAG 引擎 + 漫剧示例
  acceptance/              结果验收
  evolution/               评测集/门禁/候选生命周期/诊断案例与假设/固定探针
  release/                 发布清单/签名/回滚
  api/                     REST API（标准库实现）
  cli.py                   命令行
tests/                     92 项服务端测试
android-app/               React Native Android 客户端
  src/offline/             本地任务/事件队列、隐私过滤、同步引擎（纯 TS，可单测）
  src/device/              设备操作契约/风险闸门/原生桥接（纯 TS，可单测）
  android/                 Kotlin 原生壳：AccessibilityService + DeviceModule
  test/                    27 项客户端测试（node --test）
```
