# EvoAgent — 自进化个人智能体平台（参考实现）

依据两份基线文档实现的可运行工程：

- `EvoAgent自进化智能体产.md` — 产品需求文档（PRD 1.0，28 部分）
- `EvoAgent自进化智能体系.md` — 架构设计文档（Architecture 1.0，23 章）

条目级代码映射见 [IMPLEMENTATION.md](./IMPLEMENTATION.md)；全量功能状态（后续开发在此追加）见 [FEATURES.md](./FEATURES.md)。Android 客户端见 [android-app/](./android-app/)。

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
python -m evoagent.cli diagnose-assist <case_id>   # 模型辅助假设（需真实模型，仅 advisory）
# 候选代码隔离执行（一次性目录；无网络/子进程/凭据；超时强杀）
python -m evoagent.cli sandbox-run --code "print('hello')"
# 工作流资产库 / 预算用量 / 成本报告
python -m evoagent.cli workflows
python -m evoagent.cli budget
python -m evoagent.cli costs
# 模型迁移执行链（兼容性 → 同数据集回归 → 发布组合；--apply 灰度启用）
python -m evoagent.cli migrate mock-1 mock-2
# 记忆质量评测 / 环境漂移检测
python -m evoagent.cli memory-eval --no-persist
python -m evoagent.cli drift
# 只读运行回放 / 备份与恢复
python -m evoagent.cli replay <task_id>
python -m evoagent.cli backup create
python -m evoagent.cli backup verify <name.zip>
python -m evoagent.cli backup restore <name.zip>
python -m evoagent.cli backup list
# 成功任务 → 工作流候选（同一意图+工具序列至少重复 2 次才生成）
python -m evoagent.cli workflow-extract
python -m evoagent.cli workflow-candidates
python -m evoagent.cli workflow-approve <candidate_id> --reason "抽检通过"
python -m evoagent.cli workflow-publish <candidate_id> --args '{"step_1":{"expression":"3+4"}}'
```

诊断 REST 端点：`GET /diagnostics`、`GET /diagnostics/{case_id}`、
`GET /diagnostics/trends`、`POST /diagnostics/scan`、
`POST /diagnostics/{case_id}/experiments`（可选 `fault: error|timeout|fail` 故障注入）、
`POST /diagnostics/{case_id}/hypotheses`（模型辅助假设）、
`POST /diagnostics/{case_id}/candidates`。诊断永不自动发布；候选仍走评测门禁。

模型辅助假设只作解释、不作根因（Arch 9.4）：每条必须引用证据快照中的真实片段，
置信度封顶 0.5，不改变案例状态；与规则假设重复时跳过；未经固定探针验证的模型假设
不能升级为候选。未配置真实模型时端点返回 `503`。

模型与记忆端点：`GET /models`、`GET /models/migrations`、
`POST /models/migrations`、`POST /models/migrations/{id}/regression|release|apply|rollback`；
`POST /memory/evaluate`、`GET /memory/evaluations`；`GET /diagnostics/drift`、
`POST /diagnostics/drift/scan`（显式发出 `drift_detected` 事件）。

回放与备份端点：`GET /tasks/{id}/replay`（只读重建，需 `execution_trace` 读权限）、
`GET /admin/backups`、`POST /admin/backup`、`POST /admin/restore`（admin；有未结束任务时
`409`，恢复前自动创建安全备份）。

工作流抽取端点：`POST /workflows/extract`（可选 `min_support`/`limit`）、
`GET /workflows/candidates?status=`、`GET /workflows/candidates/{id}`、
`POST /workflows/candidates/{id}/approve|reject|publish`（admin；未批准或必填参数未绑定
时发布返回 `409`）。

只读观测端点：`GET /metrics`（JSON，`?format=prometheus` 输出文本）、`GET /audit`、
`GET /workflows`、`GET /memory/stats`、`GET /evolution/candidates`。
管理端：`GET /admin`（PRD 90.1-90.7 分区：运行健康、问题中心、诊断详情、候选实验、版本、
成本、审计与回滚，另含任务回放、备份恢复与隔离沙箱）。
设置 `EVOAGENT_API_TOKEN`（普通）或 `EVOAGENT_ADMIN_TOKEN`（管理）后，请求需带
`Authorization: Bearer <token>`。

## 接入真实 LLM（OpenAI 兼容：OpenAI/DeepSeek/Moonshot/vLLM…）

### 复用 Claude 全局配置（推荐）

`~/.claude/settings.json` 已配置 `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` /
`ANTHROPIC_MODEL` 时，只需切换到真实模型：

```powershell
$env:EVOAGENT_PROVIDER="openai"
python -m evoagent.cli task "查询现在几点了"
```

缺省的 Key、端点与模型会从 Claude 全局配置读取；`ANTHROPIC_BASE_URL` 自动补 `/v1`
（如 `https://opencode.ai/zen/go` → `https://opencode.ai/zen/go/v1`）。Key 不落仓库、
不写入日志。可用 `EVOAGENT_CLAUDE_SETTINGS` 指向其他 settings 文件。

### 显式配置任意兼容端点

```powershell
$env:EVOAGENT_PROVIDER="openai"
$env:EVOAGENT_API_KEY="sk-..."           # 显式 Key 时不复用 Claude 端点
$env:EVOAGENT_BASE_URL="https://api.deepseek.com/v1"   # 可选，自定义兼容端点
$env:EVOAGENT_MODEL="deepseek-chat"
python -m evoagent.cli task "查询现在几点了"
```

`EVOAGENT_API_KEY / BASE_URL / MODEL` 优先级最高。Mock 与真实模型共用同一适配器契约；
路由/档案/注册表逻辑一致（PRD 4.5 能力与模型解耦）。

### 记忆嵌入（可选）

默认使用零依赖哈希向量；需要语义召回时配置兼容 `/embeddings` 的端点：

```powershell
$env:EVOAGENT_EMBEDDING_PROVIDER="openai"
$env:EVOAGENT_EMBEDDING_MODEL="text-embedding-3-small"
# 缺省复用 EVOAGENT_API_KEY / EVOAGENT_BASE_URL（或 Claude 全局配置）
```

嵌入失败（网络/权限/模型不支持）时自动回退哈希向量，检索不会中断。向量随记忆持久化，
切换模型后按需重算。

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
- 候选代码在独立子进程沙箱执行：只写一次性目录、网络/子进程/沙箱外读取被审计钩子阻断、
  生产凭据不继承、超时强杀；`POST /evolution/sandbox/run` 仅 admin。
  该环境用于降低风险，不宣称是对恶意代码的强安全边界（Arch 18.3）。

## 离线优先与设备操作（PRD 64–76）

客户端（`android-app/`）在断网时仍可提交任务并持续记录事件，联网后按幂等键安全重放：

- **本地队列**：任务与事件写入单一快照（AsyncStorage），提交任务与入队事件同一次落盘；
  队列容量 500，满则拒新；`seq` 单调递增，`checkpoint` 只推进连续已确认前缀。
  每个事件带 `run_id` 与 run 内单调 `sequence_no`、不可变 `runtime_manifest_id` 和
  `clock_source=device`——因果顺序不依赖墙上时钟，服务端按序号还原。
- **幂等同步**：事件以客户端生成的 `event_id` 为主键 `INSERT OR IGNORE`，重复批次返回
  `duplicates`；任务提交带 `client_request_id`，服务端 `client_requests` 表命中即返回原
  `task_id`（`202 deduplicated`）。失败重试与崩溃恢复都不会产生重复任务。
- **回执协议**：批次号在重试间保持不变；服务端回执含 `protocol_version`、`confirmed` /
  `unconfirmed` 明确列表，客户端只删除确认事件，部分成功不会误删。
- **ack 后才删与隔离**：服务端确认前事件不删除；网络失败指数退避（2s→5min）；in-flight
  批次持久化，进程重启后回收 stale 批次继续发送；永久校验错误转隔离区保留证据，可人工
  重新入队；连续失败进死信并提供手动重试，不静默丢弃。
- **容量治理**：事件按 critical / normal / debug 分级保留（critical 永不自动清理，
  normal 7 天、debug 1 天）；达到 80% 预警；空间不足时先清理可重建的低等级事件，并写入
  含原因与事件清单的**损失标记**，界面显式提示，不做静默删除。
- **隐私保守**：仅 `standard` 事件上传；`sensitive`（设备动作、位置、记忆写入等）本地保留；
  含密码/令牌/证件号的载荷在入队前直接拒绝。
- **崩溃恢复**：`SUBMITTING` 重启后转 `NEEDS_RECONCILE`，需用户确认后重提；已下发但未验证的
  设备动作禁止自动重放。
- **版本对账**：重连后以安装实例 ID 与服务端对账，服务端检查平台、最低 App 版本、工具契约、
  制品完整性与灰度范围；客户端另做签名/摘要/交付方式/回滚路径复核，任一失败拒绝激活。

能力包更新走客户端双槽位管理器（`android-app/src/update/`）：校验 → 原子切换 → 健康检查
→ 晋级或自动回滚；原生 APK 只标记为需二进制更新，必须走平台应用更新机制。
更新分区还可同步服务端候选阶段（`GET /sync/candidates`）：只展示评测后的批准/发布/监测/
回滚状态，不改变本地激活路径。

设备操作走 Android AccessibilityService 的**节点级**通道（按文本/描述点击、拉起显式 Intent）：

- 低/中/高风险分级；高风险（发送消息、支付、删除等）必须通过独立系统确认框，不依赖模型提示词。
- 结果五态：`verified | unverified | failed | denied | unavailable`——执行后必须有证据才算成功。
- 点击/启动会采集操作前后节点快照：页面未变化、前台包名不符或出现权限弹窗时判 `unverified`，
  不假设调用成功；连续 3 次未验证自动安全停止，需人工恢复。
- 不提供坐标注入、不绕过系统权限；服务未启用时返回 `unavailable` 并引导用户去系统设置授权。

**已验证**：Gradle 构建通过，客户端已在模拟器与真机（HyperOS / Android 16）安装运行；
离线队列、事件同步、真实模型任务、无障碍节点点击与启动（`[verified]` + 前后快照证据）、
无变化降级、安全停止与人工恢复均按上述流程实测。
原生壳与 Metro 配置见 `android-app/README.md`。

## 已实现 / 迭代占位

完整矩阵见 IMPLEMENTATION.md。当前一轮：服务端核心全链路可运行 + 规则诊断/探针 + 真实模型
Planning/意图分类（复用 Claude 全局配置，已实测）+ 执行内核接线（计划驱动 ReAct/进度持久化、模型调用硬预算、路由接入运行路径、
暂停/恢复）+ 离线队列/幂等同步 + 无障碍节点操作；并完成控制面补强（API 鉴权/审计/指标、工具超时
与重试安全规则/输出契约/幂等日志、工作流并行与跳过/节点超时/检查点续跑、预算报表、记忆关系与
混合检索、质量优先路由评分、诊断趋势与探针故障注入、工作流资产库、只读 Git 适配器）；
并补齐上下文治理（装配优先级、动态预算、结构化压缩与质量检查）、评测完整性（独立保留集、
基线等价、确定性安全套件、安全证据缺失时门禁 fail-closed）、九级硬预算（单任务/单次模型调用/
单工具/工作流/候选实验/进化日与月/提供方/专家，含并发上限）、按任务/领域/模型/版本/实验的成本报告、
十维收益评价（质量/成功率/稳定性/恢复/成本/时延/安全/维护复杂度/跨领域/长期）、发布后监测
（时间窗采样、上窗对比、自动回滚判定）、四层日志（运行事件/执行轨迹/诊断证据/审计，独立保留期与
访问角色）、扩展可观测指标（模型/工具/ReAct/恢复/上下文/记忆/进化/回滚/意图维度）与分级告警、
模型迁移执行链（资产继承/兼容性/同数据集回归对比/灰度启用/回滚）、记忆质量评测与运行反馈、
环境漂移检测（工具/契约/模型/任务分布/版本变更，含处置建议与告警）、任务运行回放（只读、
敏感负载脱敏、不重放副作用）、三库备份与恢复（sha256 + 完整性校验、自动安全备份，管理端可操作）、
工具替代安全链（仅可重试失败且副作用未发生时切换，禁止副作用/权限升级）、成功任务工作流抽取
（重复支持门槛、评测与参数绑定门禁、人工批准后发布并注册运行）、候选代码隔离执行与
Web 管理端 90.1-90.7 分区（健康/问题中心/诊断详情/候选实验/版本/成本/审计回滚，含回放、备份恢复与沙箱）、
任务模型全字段（目标对象/预期输出/复杂度/所需工具与权限/网络依赖/工作流适用性/待确认问题，显式输入优先）、
用户端七个中心页面（任务/离线状态/工作流/记忆/预算与模型/权限/更新；记忆支持纠正与删除并审计，
工作流展示版本/运行/产物，预算模型与更新只读展示）、可插拔记忆嵌入（默认哈希，OpenAI 兼容端点，
向量持久化与失败回退）、发布分支流程（前缀白名单、隔离分支、轻量标签、`--ff-only` 合入 main，
显式 CLI 与审计）、无障碍界面观测（操作前后节点快照、页面变化与权限弹窗识别、连续未验证安全停止）、
双端版本对账与运行清单（安装实例/设备身份、批次回执、`/sync/reconcile`、`/sync/apply-result`、
`/sync/candidates` 候选阶段同步）、客户端更新管理器（双槽位原子切换、健康检查与自动回滚）、
离线诊断包受控导入（解压防护/摘要校验/关系审查/按 SHA-256 幂等入隔离区，仅证据不执行）。
测试通过：Python 344 项、客户端 69 项。迭代占位：视觉/OCR 操作、后台常驻同步、真实生成服务适配、能力包制品装载。

## 目录

```
evoagent/                  Python 服务端核心
  core/                    任务模型/状态机/控制器/预算/权限/检查点/事件/持久化
  planning/  react/        Planning 与 ReAct 引擎
  models/                  Mock + OpenAI 兼容适配器 + 路由 + 模型注册表 + 迁移执行链
  tools/                   工具契约/注册表/内置工具（含只读 Git）
  memory/                  记忆存储（关系/混合检索）、上下文装配与质量评测
  experts/                 专家注册表（首批 5 个专家）
  workflows/               工作流 DAG 引擎 + 资产库 + 成功任务抽取 + 漫剧示例
  acceptance/              结果验收
  evolution/               评测集/门禁/候选生命周期/诊断案例与假设/固定探针/漂移检测/隔离沙箱
  release/                 发布清单/签名/回滚 + 发布分支/标签/快进合并
  api/                     REST API（标准库实现）
  core/replay.py           只读运行回放
  core/backup.py           三库备份/校验/恢复
  cli.py                   命令行
tests/                     344 项服务端测试
android-app/               React Native Android 客户端
  src/offline/             本地任务/事件队列、隐私过滤、同步引擎（纯 TS，可单测）
  src/device/              设备操作契约/风险闸门/界面观测与结果验证/原生桥接（纯 TS，可单测）
  src/update/              安装实例/版本对账/双槽位更新管理（纯 TS，可单测）
  android/                 Kotlin 原生壳：AccessibilityService + DeviceModule
  test/                    69 项客户端测试（node --test）
```
