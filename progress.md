# EvoAgent 实施进度日志

## Session 1 (2026-10-20)

### 背景
- 输入文档：PRD 1.0（28 部分，140+ 项能力）、Architecture 1.0（23 章）。
- 用户决策：Python 核心/完整功能先行/双模型适配器/Android 用 React 系框架。
- 规模结论：一次交付 = 服务端核心全链路可运行 + 存量子系统的可工作简化实现 + Android 骨架，后续迭代补强。

### 完成事项
- [x] 规划文件建立（task_plan / progress）
- [x] 环境确认：Python 3.10.11，无第三方依赖假设（全标准库实现；requests/pytest 可选）
- [x] 工程目录与配置
- [x] 核心内核（任务模型、状态机、预算、权限、事件、检查点、SQLite 持久化）
- [x] 运行时控制器 + ReAct + Planning
- [x] 模型层（Mock + OpenAI 兼容 + 路由 + 模型注册表）
- [x] 工具系统（契约/注册表/生命周期/6 个内置工具/副作用分级）
- [x] 记忆与上下文（SQLite 记忆库、可信状态、装配/压缩）
- [x] 专家注册表 + 首批 5 个专家配置
- [x] 工作流 DAG 引擎 + 漫剧示例
- [x] 验收器 + 评测集/门禁 + 进化候选生命周期
- [x] 发布管理（清单/门禁/签名/回滚）
- [x] REST API（stdlib http.server，JSON over HTTP）
- [x] CLI（demo/run/status）
- [x] 测试 21 项全部通过（unittest 风格，pytest 可跑）
- [x] Android React Native 骨架（App.tsx、API 客户端、manifest、构建说明）
- [x] IMPLEMENTATION.md 可追溯性映射

### 测试结果
- `python -m pytest tests/ -q` → 51 passed（状态机/预算/工具契约与权限/运行时/记忆/工作流/进化门禁/发布管理/API）
- demo 冒烟：`python -m evoagent.cli demo` 3 个 ReAct 任务 SUCCEEDED；`workflow` 漫剧 DAG SUCCEEDED
- 客户端 API 冒烟：/health、/tasks、/tasks/{id}/events、/cancel 全部通过 test_api.py

### 本轮修复的缺陷（全部有对应测试回归）
- memory_items 列数不匹配 + 补 superseded_by + 旧库自动重建
- tasks 表缺 created_at + 旧库自动重建
- execute() 在早期失败后未中止导致状态机冲突
- Verifier 空 criteria 默认拒绝 → 改默认 result_nonempty
- MockModel intent 解析含 domain 后缀失败
- 工具副作用登记与权限确认链（file_write 需确认）
- 上下文压缩保留硬约束；中文 bigram 检索
- API /tasks/{id}/events 路由解析；file_write 工作目录隔离

### 遗留/待迭代
- Android 侧需在 Android Studio / RN CLI 真机构建验证（当前环境无模拟器）
- openai_compat 适配器需真实 Key 联调
- 进化系统的候选生成目前是启发式 + 评测门禁，真实"隔离环境跑代码"需沙箱轮
- Web/管理端（PRD 90）未做，API 已留好界面数据接口
- 无障碍/语音/本地任务队列（PRD 64/68/72-76）待 Android 迭代轮

## Session 2 (2026-10-20)

### 完成事项
- [x] Git 基线（`.gitignore` + `main` 分支基线提交）
- [x] 诊断模型：`FaultCategory` A/B/C/D、案例/假设/实验数据结构、证据→多假设规则表
- [x] 失败自动诊断钩子：`_finalize` / 规划失败 / 控制器异常都会沉淀案例，`task.runtime_manifest` 记录 case_id
- [x] 固定探针 6 个（tool_health / calc_roundtrip / file_write_sandbox / state_machine_selfcheck / memory_roundtrip / context_constraint_retention），超时转不确定、无外部网络
- [x] 诊断持久化：SQLite 三表 + CRUD（案例/假设/实验）
- [x] 有界实验：每案 3 次、每日 50 次、单探针 10s；探针只调分不宣布根因
- [x] 假设→候选显式升级（`candidate_from_hypothesis`），风险 L1-L4 映射，候选仍需门禁/发布流程
- [x] REST 五个诊断端点 + CLI `diagnose [task_id] [--probe]`
- [x] 真实模型 Planning（JSON 计划校验、坏计划可追溯回退模板）
- [x] 新增意图分类器（hint 短路 / 模型 JSON / 启发式回退 / 低置信标记）
- [x] 测试 75 项全部通过（新增 24 项：诊断 13 + 规划/意图 11）

### 测试结果
- `python -m pytest tests/ -q` → 75 passed
- `python -m evoagent.cli demo` / `workflow` 行为不变
- CLI 诊断冒烟：失败任务 → 案例 2 条假设 → `--probe tool_health` 通过并降低环境假设分数

### 设计约束（本轮确立）
- 规则优先：假设由确定性规则从证据生成，模型不参与根因判定（Arch 9.4）
- 永远多假设：不允许单条"就是提示词问题"结论；保留支持/反对证据与不确定性
- 置信度是评分不是校准概率；上限 0.90
- 诊断不发布：候选需显式升级，发布仍走评测门禁与 L0-L4 规则
- 探针隔离：临时目录本地副作用，禁止外部网络

### 遗留/待迭代（更新）
- LLM 辅助诊断假设生成与实验建议（当前规则+探针闭环）
- 真实 Key 联调验证 Planning/意图分类 JSON 输出
- Android 真机构建、无障碍/语音/本地队列
- 候选代码隔离沙箱、Web 管理端、向量检索

## Session 3 (2026-10-20)

### 完成事项
- [x] 服务端事件同步：`runtime_events` 旧库自动补 5 列；`EventBus.ingest()` 以 `event_id` 为主键
      `INSERT OR IGNORE`；`POST /events/sync` 批量摄取，返回 accepted/duplicates/rejected
- [x] 批次校验 `api/events_sync.py`：1–200 条、event_id 格式、±7 天时钟窗、payload ≤16 KiB、
      severity 白名单；非 `standard` 隐私级别一律拒绝
- [x] 任务幂等提交：`client_requests` 表 + 提交锁；同 `client_request_id` 二次提交返回原 task_id
      （`202 deduplicated`），并发两次也只产生一个任务
- [x] 客户端离线核心（`src/offline/`）：注入式 KeyValueStore、整快照原子写、事件队列（FIFO/去重/
      容量 500/死信）、连续 checkpoint、隐私分类过滤、指数退避 2s→5min（带抖动）
- [x] 本地任务库：离线提交 `LOCAL_ONLY`；上线以稳定 `client_request_id` 转 `SUBMITTING`；
      `SUBMITTING` 崩溃恢复转 `NEEDS_RECONCILE` 需用户确认；状态轮询映射
- [x] 离线可执行策略：`time_query` 直接用设备本地时间完成，不上传
- [x] 设备操作（`src/device/`）：风险分级、高风险独立确认、journal `prepared→dispatched→…`、
      已下发未验证禁止重放、结果五态、无 NativeModule 返回 `unavailable`
- [x] Android 原生壳修复：manifest 合法权限、`MainApplication`/`MainActivity`、Gradle 与 RN 0.75
      模板对齐；`DeviceAccessibilityService`（按文本/描述点击，无手势注入）+ `DeviceModule`
- [x] `App.tsx` 集成：在线/离线状态条与手动同步、队列统计、本地任务、死信重试、设备操作面板
- [x] 测试 83 项 Python（新增 8 项事件同步/幂等）+ 27 项客户端（离线 19 + 设备 8）

### 测试结果
- `python -m pytest tests/ -q` → 83 passed
- `cd android-app && npm run test:offline` → 27 passed
- `python -m evoagent.cli demo` / `workflow` 行为不变
- API 冒烟：同 `client_request_id` 两次提交返回同一 task_id；重复事件批次返回 duplicates；
  sensitive 事件 400 且带逐条原因

### 本轮修复的缺陷
- `ApiServer.shutdown()` 未 `server_close()`：Windows 上 SO_REUSEADDR 导致端口被占用后新实例
  bind 成功但不服务（测试间互相干扰）→ shutdown 与 serve_forever 退出路径都关闭套接字
- Node 24 type-stripping 不支持参数属性 → 所有类改为显式字段赋值
- checkpoint 在全部确认后少 1 → 无待传事件时取 `next_seq`
- 一批含被拒事件时同轮二次重试 → 处理后立即结束本轮 flush

### 设计约束（本轮确立）
- 幂等以服务端为准：客户端重试永远安全，不依赖客户端状态完整
- ack 前不删、checkpoint 不跳空洞、失败进死信不静默丢弃
- 敏感事件不出设备；secret/private 载荷入队前拒绝
- 高风险设备操作必须独立确认；结果无证据只能 `unverified`，且不得自动重放
- 无障碍通道不做坐标/手势注入，不绕过系统权限

### 遗留/待迭代（更新）
- Android 真机构建与验收（本机无 SDK/模拟器，Gradle 构建与无障碍行为均未验证）
- 视觉/OCR、语音、后台常驻同步；端到端加密与多设备冲突合并
- LLM 辅助诊断假设、真实 Key 联调
- 候选代码隔离沙箱、Web 管理端、向量检索

## Session 4 (2026-10-20)

### 背景
- 差距审计后的 P0：执行内核补真——规划真正驱动执行、预算真正计量、路由真正生效、
  暂停/恢复可用（PRD 11-12、19、34-36、55-56）。

### 完成事项
- [x] Planning 驱动 ReAct：计划以消息注入首轮；每个成功工具步推进 `plan_progress`；
      持久化到 `runtime_manifest.execution_plan` / `plan_progress`（done/failed/pending/change_reasons）
- [x] 失败步回退：追加恢复步骤并通过 `plan.adjust()` 记录变更原因，原目标与硬约束不变（PRD 11.2）
- [x] 预算计量：规划与 ReAct 共用同一 `BudgetController`；每次模型调用先 `check_model_call()`
      再 `record_model_call(估算成本)`；`max_model_calls=1` 时任务以预算原因 FAILED
- [x] 路由接入运行路径：控制器按 intent/domain 调用 `ModelRouter.route()`，选择结果记录到
      `runtime_manifest.model_route`；适配器按 `model_id` 解析，未命中回退 Mock
- [x] 暂停/恢复：`pause_task` 置标志，ReAct 在模型调用前/建议后检查，暂停后不再执行工具；
      SUSPENDED 检查点保存累计预算；恢复时从检查点还原 usage
- [x] 副作用闸门：存在 `pending_side_effects(state=unknown)` 时恢复必须显式 `force`（PermissionError）
- [x] FAILED 重跑：生成新任务（新 task_id + `parent_task_id` + `resumed_from`），终态不可变
- [x] API：`POST /tasks/{id}/pause`、`POST /tasks/{id}/resume`（409 带 `requires_force`）
- [x] CLI：`pause <task_id>`、`resume <task_id> [--force]`
- [x] 新增 `tests/test_control.py` 9 项：计量、限额、计划持久化、路由、暂停恢复、重跑、闸门

### 测试结果
- `python -m pytest tests/ -q` → 92 passed
- `cd android-app && npm run test:offline` → 27 passed
- API 冒烟：任务 SUCCEEDED；`execution_plan` 已持久化；`model_route.model_id=mock-1`；
  同 `client_request_id` 返回 deduplicated；终态任务 pause/resume 均 409

### 设计约束（本轮确立）
- 一个任务一个预算对象：规划、React、恢复后都计入同一累计用量
- 暂停语义是“不再发起新的模型调用与工具执行”，已开始的单步执行完再落检查点
- 恢复不盲重放：未知副作用需用户显式 force；FAILED 只重跑为新任务，不改写历史
- 计划进度只由成功工具步推进，失败步显式标记并追加恢复步骤

### 遗留/待迭代
- 恢复目前是“从下一步继续”而非精确到迭代内断点；长任务恢复粒度可再细化
- 真实模型 Key 联调下的暂停时延与取消传播待验证
- 视觉/OCR、后台常驻同步、Web 管理端等保持原计划

## Session 5 (2026-10-20)

### 背景
- 差距审计结论：执行内核已真跑，但工具/工作流/预算、记忆/路由/诊断、控制面与客户端观测仍偏浅。
- 目标：把可本地实现的能力补齐并配测试，环境受限项如实标注。

### 完成事项
- [x] 控制面：`GET /metrics`（JSON + Prometheus，含 `# TYPE`）、`GET /audit`、`GET /admin`；
      GET/POST 鉴权（`EVOAGENT_API_TOKEN` / `EVOAGENT_ADMIN_TOKEN`，Bearer）；
      进化 propose/evaluate/approve/deploy/monitor/rollback 全部写审计
- [x] 工具补强：工作线程实现真实超时；仅无副作用或带幂等键的工具可重试；输出 schema 校验；
      幂等执行日志持久化（命中回放标记 `replayed`）；新增只读 `git_read`（固定子命令、参数白名单、禁 shell/网络）
- [x] 工作流补强：按依赖分层并行、条件跳过（`SKIPPED`）、节点超时、逐批检查点与续跑
      （复用 `workflow_outputs`）；暂停任务转 `SUSPENDED`；工作流定义可序列化进资产库
- [x] 预算报表：`BudgetLedger.report()`（runtime/evolution 日/月池）+ `cli budget`
- [x] 记忆治理：关系（refines 等）持久化与查询、使用计数、按 kind/scope/status 统计；
      词法 + 哈希向量混合相关性；上下文装配统计并回写使用计数
- [x] 路由评分：质量 0.70 / 成本 0.15 / 时延 0.15，可用性过滤 + 显式路由优先 + `rank()` 解释
- [x] 诊断深度：`GET /diagnostics/trends`；探针支持 `error/timeout/fail` 故障注入
- [x] 工作流资产库：`workflows/library.py` 持久化/加载 + API/CLI `workflows`
- [x] 客户端只读系统状态面板：任务指标、预算、发布/回滚、进化候选（`src/api.ts` 新增 `metrics()`/`candidates()`）
- [x] 测试：Python 123 项（新增 31 项：控制面 9、工具/工作流 11、记忆/路由/诊断 11）；客户端 27 项

### 测试结果
- `python -m pytest tests/ -q` → 123 passed
- `cd android-app && npm run test:offline` → 27 passed
- `python -m evoagent.cli demo` / `workflow` 行为不变

### 本轮修复的缺陷
- `validate_against_schema` 仅在存在 `properties` 时检查 `required` → 两者任一存在即检查
- `record_use` 更新不存在的列 → 改为写 `extra` JSON；`stats` 同步修正
- `score()` 成本未归一化导致低价模型压过高质量模型 → 归一化 + 质量权重提高
- Prometheus 输出缺少 `# TYPE` 行 → 每个指标首次出现时声明一次
- 非 UTF-8/非对象 JSON 请求体导致处理器抛异常并空响应 → 统一按空对象处理，返回 400 且服务不中断

### 设计约束（本轮确立）
- 质量优先：成本/时延只在质量可接受时作为次要因素，禁止“因整体便宜而无条件切换”
- 重试安全：有外部副作用的工具只有在幂等键下才允许重试
- 工作流续跑只复用上次已完成的节点输出，版本不一致则从头执行
- 观测面只读；所有变更类接口必须鉴权并写审计

### 遗留/待迭代
- Android 真机构建与验收；本机无 `node_modules`，`tsc --noEmit` 未能运行（客户端测试走 Node 24 type-stripping）
- 真实 LLM Key 联调；视觉/OCR/语音、后台常驻同步、端到端加密
- 嵌入向量检索（当前为词法 + 哈希向量）、Web 管理端、候选代码隔离沙箱
