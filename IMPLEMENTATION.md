# IMPLEMENTATION — PRD / 架构条目 ↔ 代码可追溯性映射

目标（PRD 114）：全量能力保留在统一规划与架构依赖图中；本文档记录每个能力域的实现状态。
状态说明：✅ 本轮可用（含测试）｜🔹 接口已留、实现待迭代｜⬜ 未开始（在迭代计划中）。
逐功能的当前状态、接口、配置与维护约定见 [FEATURES.md](./FEATURES.md)。

基线文档：`EvoAgent自进化智能体产.md`（PRD 1.0）、`EvoAgent自进化智能体系.md`（Architecture 1.0）
及三份补充设计（映射见下方专节）：`EvoAgent_双端协同进化与版本分发架构补充设计.md`、
`EvoAgent_服务端逻辑服务划分与协同机制.md`、`EvoAgent_离线日志与可靠同步子系统设计.md`。

## 服务端核心（Python, `evoagent/`）

| PRD / Arch 条目 | 模块 | 状态 | 说明 |
|---|---|---|---|
| PRD 7 统一任务入口 / Arch 3.1 任务模型 | `core/task.py`, `core/controller.py` | ✅ | 统一任务实体、分类→规划→执行→验收；PRD 7.3 任务解析全字段（目标对象/预期输出/复杂度/所需工具与权限/网络依赖/工作流适用性/待确认问题），显式输入优先，规划与工具契约补齐空值 |
| PRD 8 意图识别与路由 | `experts/registry.py` route(); `planning/intent.py` | ✅ | 显式 hint 优先；模型 JSON 分类限定已注册意图；失败回退关键字启发式；低置信 + 高风险在控制器暂停 |
| PRD 9-10 领域专家/能力依赖图 | `experts/` | ✅ | 注册表 + 5 专家 + 依赖边 + impact_analysis |
| PRD 11-13 Planning/ReAct/工作流 | `planning/`, `react/`, `workflows/` | ✅ | 模型 JSON 计划（≤12 步/唯一 id/依赖存在/无环）失败可追溯回退模板；计划注入 ReAct 并持久化进度（done/failed/pending）；失败步回退调整保留原目标与硬约束；防循环（12.3）、固化工件（13.4 有验收门禁）；工作流 DAG 按依赖分层并行、条件跳过、节点超时、逐批检查点与续跑（复用已完成输出）；资产库序列化/加载 |
| Arch 16.2 成功任务 → 稳定工作流 | `workflows/extraction.py`, `workflows/library.py` | ✅ | 按 `(intent, 成功工具序列)` 聚合成功任务，默认至少 2 次支持；排除评测来源与已有工作流任务；结构/工具可用性/必填参数绑定评测（`runnable`）与成本对照；`candidate → approved → published` 人工门禁并写审计；发布绑定参数后写入资产库并注册运行时调度 |
| PRD 14-16 工具生命周期/契约/恢复 | `tools/` | ✅ | 契约校验、五层结果区分（15）、错误分类；真实超时（工作线程）；仅无副作用或带幂等键的工具重试；输出契约校验；幂等执行日志持久化；只读 Git 适配器（白名单子命令，禁 shell/网络）；Arch 6.4 工具替代：仅可重试失败且副作用未发生时按声明顺序尝试备用工具，拒绝副作用/权限升级与输入不兼容，副作用不确定即停止，过程写入证据 |
| PRD 17-20 Harness/模型管理 | `react/engine.py`, `models/`, `config.py` | ✅ | 适配器契约统一；模型档案/路由；运行时按 intent/domain 路由，选择结果写入 `runtime_manifest.model_route`；质量优先加权评分（质量 0.70 / 成本 0.15 / 时延 0.15）与 `rank()` 决策解释，显式路由与可用性过滤优先；模型迁移九步执行链（档案与资产继承 → 兼容性 → 同数据集回归对比 → 发布组合 → 受控启用 → 回滚），固定模型覆盖解析失败时回退默认路由并写告警事件；`EVOAGENT_PROVIDER=openai` 时缺省凭据/端点/模型自动读取 Claude 全局 `settings.json`（显式环境变量优先，Key 不落仓库），真实模型 Planning/意图分类/ReAct 已联调 |
| PRD 21-28 记忆系统 | `memory/store.py`, `memory/embeddings.py`, `memory/evaluation.py` | ✅ | 类型/归属/可信状态/写入流程/冲突替代/过期；记忆关系（refines 等）持久化与查询；词法 + 向量余弦混合相关性；可插拔嵌入（默认哈希，`openai` 兼容端点），向量持久化、模型切换重算、失败回退哈希；使用计数与 kind/scope/status 统计；质量评测（写入准确率/召回/精确率/MRR/过期误用/冲突正确性）与运行反馈（有/无记忆成功率对比、有害/误导率），报告持久化 |
| PRD 29-33 上下文治理 | `memory/context.py` | ✅ | 装配优先级（约束/状态/验收永不裁剪）、动态预算（模型 context_limit 约束）、结构化压缩 + 自然语言摘要、质量检查（约束保留/跨域污染/不可信指令/溢出/状态缺失）；装配统计并回写记忆使用计数 |
| PRD 34-36 检查点/恢复/取消 | `core/checkpoint.py`, `core/controller.py` | ✅ | 恢复先验证副作用状态（35），未知副作用需显式 force；暂停/恢复共享累计预算；FAILED 恢复生成带 `parent_task_id` 的新任务；取消阻止后续动作 |
| PRD 37-40 运行日志/可观测 | `core/events.py`, `core/log_layers.py`, `core/metrics.py`, `core/alerts.py`, `api/server.py` | ✅ | 事件模型字段与 Arch 9.1 对齐；旧库自动补列；客户端事件批量幂等摄取；四层日志（runtime_event 30d / execution_trace 14d / diagnostic_evidence 7d / audit 365d，Arch 9.2）独立存储、角色访问与保留清理；证据导出需 standard + 授权；执行轨迹按任务连续 step；扩展指标（模型/工具/ReAct/恢复/上下文/记忆/进化/回滚/意图维度，Arch 20.1-20.2）与 9 条分级告警（含 `environment_drift`）；`GET /metrics`（JSON + Prometheus，无样本输出 NaN）、`/alerts`、`/logs/*`、`GET /audit`、`GET /admin`；清理/删除写审计 |
| Arch 20.3-20.4 运行回放/备份恢复 | `core/replay.py`, `core/backup.py`, `api/server.py`, `api/admin.py` | ✅ | 回放从事件/执行轨迹/检查点/运行记录重建只读时间线（`read_only`、`reexecute_side_effects=false`），非 `standard` 负载脱敏；备份用 SQLite 在线备份 API 打包三库 + `manifest.json`（sha256/行数/发布版本），校验含完整性检查，恢复前自动安全备份且拒绝存在未结束任务；管理端提供回放与备份/恢复面板 |
| PRD 41-45 诊断/故障分类 | `evolution/diagnostics.py`, `evolution/probes.py`, `evolution/drift.py` | ✅ | 失败自动沉淀持久化案例；规则从证据生成 A/B/C/D 多假设（支持/反对证据+不确定性）；6 个固定探针有界无外网，支持 `error/timeout/fail` 故障注入；趋势聚合（按状态/意图/类别/组件）；假设显式升级候选；LLM 辅助假设（Arch 9.4）：每条必须引用证据快照片段、置信度封顶 0.5、不改案例状态，且须经固定探针验证后才能升级候选；环境漂移检测（PRD 44）：工具成功率/契约错误/模型行为/任务分布/组件与依赖版本，当前窗 vs 基线窗，输出处置建议并接入指标/告警/API |
| PRD 46-50 自进化引擎 | `evolution/` | ✅ | 候选生命周期状态机与 Arch 10.4 完全一致；十维收益向量（50）：质量（难度加权）/成功率/稳定性代理/恢复/成本/时延/安全/维护复杂度/跨领域/长期，缺证据标 `insufficient_evidence`；候选代码隔离执行（Arch 18.3）：一次性目录、环境白名单、审计钩子阻断网络/子进程/沙箱外读写、超时强杀 |
| PRD 51-54 评测体系/门禁 | `evolution/evaluator.py`, `evolution/safety.py` | ✅ | 五层架构中第 3/4 层实现；数据集元数据/版本/适用范围与独立保留集（PRD 52）；基线数据集/版本不等价直接拒绝；安全证据由确定性套件产出，缺失时门禁 fail-closed（Arch 11.5）；覆盖硬难度回归、约束违规、工具失败率、恢复成功率、成本/时延 |
| PRD 55-58 成本控制 | `core/budget.py` | ✅ | 九级硬预算：单任务（模型/工具/成本/时长/并发）、单次模型调用成本、单工具（次数/成本，可覆盖）、工作流作用域、候选实验、进化日/月池、模型提供方池、领域专家池；规划与 ReAct 共用同一预算并逐次计量，超限 FAILED；成本报告按任务/领域/模型/版本/候选实验（成本增量、质量增益、失败浪费），经 `GET /costs` 与 `cli costs` 暴露 |
| PRD 59-63 Git/版本/发布 | `release/manager.py`, `release/git_flow.py` | ✅ | 清单、签名、验证、灰度接口、回滚；隔离发布分支（前缀白名单）、轻量标签、`--ff-only` 合入 main；显式 `release-git` CLI 与审计，不自动触发/推送 |
| PRD 64-71 Android | `android-app/` + `android/` | ✅/🔹 | RN 壳 + REST；无障碍节点操作（按文本点击、显式 Intent、节点快照）经 AccessibilityService + DeviceModule；点击/启动以操作前后快照判定页面变化，识别权限弹窗，连续未验证触发安全停止；五态结果与风险闸门；视觉/OCR/语音操作待迭代 ⬜ |
| PRD 72-76 离线与双端 | `android-app/src/offline/`; `api/events_sync.py` | ✅ | 本地任务/事件队列（容量、去重、连续 checkpoint、死信）、隐私过滤、指数退避、断点续传；事件与任务两端幂等；`SUBMITTING` 崩溃恢复需用户对账 |
| PRD 77-79 能力包/AI 漫剧 | `workflows/builtin.py` | ✅ | 漫剧 DAG 示例 + 服务适配器可替换（78.4） |
| PRD 80-82 风险分级与发布 | `evolution/controller.py`, `evolution/monitor.py` | ✅ | L0-L4 规则表（81）；发布后监测（82）：时间窗采样、上窗对比、评估任务剔除、安全事件即时回滚、warning hold、`auto_rollback` 可关闭 |
| PRD 83-88 权限/安全/隐私 | `core/permissions.py` | ✅ | 最小权限、高风险确认、外部内容信任边界原则落实在工具层 |
| PRD 89-90 界面 | `api/server.py`, `api/admin.py` + `android-app/App.tsx` | ✅ | 客户端七分区：任务（在线状态/同步统计/本地任务/死信/设备操作/系统状态）、离线状态、工作流中心（版本/运行/产物）、记忆中心（过滤/纠正/删除，权限范围 403 与审计）、预算与模型（只读）、权限（无障碍状态与系统设置引导）、更新（版本/清单/兼容性/回滚）；Web 管理端覆盖 90.1-90.7：健康与告警、问题中心与诊断详情（假设/证据/不确定性/探针/模型辅助假设）、候选实验中心、版本中心（发布/模型/迁移/工作流）、成本中心、审计与发布回滚，另有回放/备份/沙箱分区 |
| PRD 91 数据实体 | `core/persistence.py`, `memory/store.py` | ✅ | 逻辑实体的核心子集 |
| PRD 92-99 非功能需求 | 各模块内 | ✅/🔹 | 稳定性→检查点；性能/成本计量在列 |
| PRD 106-113 验收标准 | `tests/`, `android-app/test/` | ✅ | Python 352 项：任务执行/预算/安全/回滚/验收/诊断闭环与趋势/模型辅助假设证据绑定与探针门禁/候选代码隔离执行/规划与意图分类/事件同步幂等与批次回执/诊断附件分块上传与摘要校验/版本对账与应用回执/候选阶段同步/运行清单/计划与预算接线/暂停恢复/鉴权与审计/指标/工具超时重试幂等/工具替代安全链/工作流并行跳过续跑/成功任务抽取与发布门禁/上下文治理与质量检查/评测完整性与 fail-closed 门禁/九级预算与成本报告/十维收益与发布后监测/路由评分/资产库/日志分层与告警/模型迁移执行链/记忆评测/嵌入向量检索/环境漂移/运行回放/备份恢复/管理端分区与安全渲染/发布分支与快进合并/Claude 全局模型配置/任务模型全字段/用户端中心接口/诊断包受控导入；客户端 98 项：离线队列/同步/任务状态/保留等级与损失标记/本地回执与不可用告警/附件分块上传与续传/同内容重新选择复用记录/设备闸门/界面观测与结果验证/更新管理器与候选阶段同步/诊断包导出与导入 |
| PRD 115 实施顺序 | Phase 1-2 全部 | ✅ | 阶段三（进化闭环）部分完成 |

## PRD 覆盖率盘点（2026-10-20）

按 `FEATURES.md` 功能矩阵统计：**206 项 ✅、0 项部分、4 项待迭代**（视觉/OCR/语音、
后台常驻同步、端到端加密与多设备冲突合并、远端推送与冲突自动合并；另有 1 项明确不做：
坐标注入/截屏模拟，不绕过系统权限）。服务端主链路（PRD 7-63、80-88、100-113）已闭环；
剩余差距集中在客户端交互与外部集成，不影响现有运行与安全边界。
三份补充设计引入的目标约束（能力包下发、App 侧安全激活、版本对账、离线可信同步等）映射见下文
「补充设计 A/B/C 映射」三节；版本对账、运行清单、双槽位更新管理器、回执与候选阶段同步已完成；
人工诊断通道（离线导出/双端受控导入/附件分块上传）已闭环，剩余为能力制品装载。

| 剩余缺口 | PRD / Arch | 规模 | 当前状态 | 备注 |
|---|---|---|---|---|
| 视觉/OCR 操作、语音入口 | PRD 64/69 | L | 仅无障碍节点 + 显式 Intent | 需设备侧识别与语音通道；不得绕过系统权限 |
| 后台常驻同步 | PRD 72-76 | M | 前台 + 手动重试 | 需 Android 后台任务与电池/网络策略 |
| 端到端加密、多设备冲突合并 | PRD 72-76/86 | L | 无 | 涉及密钥管理与冲突模型 |
| 远端推送、冲突自动合并 | PRD 59-63 | M | 仅本地分支/标签/ff 合并 | 不自动 push；分叉即失败，需人工处理 |

**实现规模参考**：服务端约 16.6k 行 Python（含 CLI），Android 客户端约 4.8k 行 TS/Kotlin，
测试约 7.9k 行；Python 352 项 + 客户端 98 项。

## 补充设计 A 映射：《双端协同进化与版本分发架构补充设计》

| 补充 A 条目 | 模块 | 状态 | 说明 |
|---|---|---|---|
| §1/§6 失败→改善闭环 | `evolution/` 全链 + `release/monitor.py` + `android-app/src/update/` | ✅ | 服务端半环完整（证据→诊断→候选→门禁→发布→监测→回滚）；App 侧对账-校验-激活-健康检查-回滚-回执已闭环 |
| §2 Evolution Sync 与进化控制器分离 | `api/events_sync.py`, `api/auth.py`, `release/delivery.py` | ✅ | 事件摄取校验/幂等/回执独立于进化模块；应用结果回传；候选状态同步（服务端→App，`GET /sync/candidates`） |
| §2.1 App 稳定执行端职责 | `android-app/src/offline/`, `src/update/` | ✅ | 持久化队列/隐私过滤/断点续传/离线任务；更新验证-激活-回滚-健康检查与回执 |
| §2.2 服务端诊断与进化中心 | `evolution/diagnostics.py`, `probes.py`, `drift.py`, `sandbox.py` | ✅ | 隐私过滤归档、组件组合还原（manifest 关联）、案例多假设、隔离执行 |
| §3.1 运行身份信息九类 | `core/events.py` + `task.py` + `api/server.py` | ✅ | 事件字段含 correlation/causation、验收/资源证据；device_id / installation_id / app_version 经 `/tasks` 校验落库 |
| §3.2 Runtime Manifest 可追溯 | `core/controller.py`, `runtime_manifests` | ✅ | model_route / execution_plan / plan_progress / diagnostic_case_id / context / resumed_from / workflow_budget；每次运行另存独立清单（task_id/run_id/设备身份/组件版本），`GET /tasks/{id}/manifests` 可查 |
| §3.3 事件队列与上传协议 | `android-app/src/offline/queue.ts`, `sync.ts` + `api/events_sync.py` | ✅ | 持久化队列→批次上传（privacy 过滤）→服务端幂等回执→ack 后才删；重复上传幂等；死信保留 |
| §4 四类更新分类 | `models/migration.py`, `workflows/library.py`, `release/delivery.py` | ✅/🔹 | 模型迁移含兼容检查/回归对比/受控启用/回滚；能力包可声明 `bundle_kind` 并按槽位分发；实际制品下载/加载由 OTA 承载（未接线）🔹 |
| §5.1 发布清单字段 | `release/manager.py`, `release/git_flow.py` | ✅ | release_id/版本/摘要/签名/回滚目标/依赖组件 + 目标平台/最低 App 版本/工具契约/通道/包类型 |
| §5.2 App 更新六步检查 | `release/delivery.py`, `android-app/src/update/manager.ts` | ✅ | 平台/契约/最低版本/签名/摘要/回滚路径检查；客户端不信任单一来源，任一失败拒绝激活 |
| §5.3 双版本槽位原子切换 | `android-app/src/update/manager.ts` | ✅/🔹 | a/b 槽位原子切换 + last known good + 健康检查失败自动回滚 + 回执；实际包内容装载由 OTA 层负责（未接线）🔹 |
| §7 离线与版本对账场景表 | `android-app/src/offline/`, `src/update/` | ✅ | 离线继续执行、积压补传、更新校验失败保持稳定版；重连后按 `installation_id` 对账选目标版本 |
| §8 逻辑数据实体 13 项 | `core/persistence.py`, `release/manager.py` | ✅ | runtime_manifests / release_manifests / rollback_records / 诊断案例 / 评测记录 / deployment+rollback；device_installations / device_deployments / sync_batches（含回执）/ device_candidate_states |
| §9 四条架构规则 | 决策记录 + 各模块 | ✅ | 版本可追溯（manifest）、门禁 fail-closed、App 不自修改（高风险默认确认）、改进不跨域（impact_analysis）均已承接 |
| §10 模块边界与接口 | `api/`（同步服务）、`release/`（分发） | ✅ | 分发服务按平台/App 版本/工具契约/通道/灰度筛选适用更新 |
| 附录 A 十步端到端协议 | 全链 | 1-9 ✅ / 10 🔹 | 步 8-9（App 激活、健康检查与回传/回滚）已闭环；步 10「按真实表现自动晋级灰度」仍由人工/监测判定 🔹 |

## 补充设计 B 映射：《服务端逻辑服务划分与协同机制》

八个逻辑服务对现有代码的逻辑视图（文档明确：逻辑先行、不要求独立部署；当前为模块化单后端）：


| 逻辑服务 | 现有模块 | 状态 | 边界说明 |
|---|---|---|---|
| 1. 任务编排服务 | `core/controller.py`, `core/state_machine.py`, `planning/`, `workflows/engine.py` | ✅ | 权威任务状态在运行时控制器 |
| 2. 模型网关服务 | `models/router.py`, `openai_compat.py`, `metering.py`, `migration.py` | ✅ | 外部模型 API 为外部依赖；路由/账单/迁移在此 |
| 3. 能力与工具服务 | `tools/`, `experts/`, `workflows/library.py` | ✅ | App 端设备操作不在此层（DeviceModule 在客户端） |
| 4. 记忆与上下文服务 | `memory/` | ✅ | 事实/证据/假设区分由诊断层承接 |
| 5. 同步与观测服务 | `api/events_sync.py`, `core/events.py`, `log_layers.py`, `metrics.py`, `alerts.py` | ✅ | 幂等/断点续传/隐私过滤/批次回执/保留策略已实现 |
| 6. 进化与诊断服务 | `evolution/diagnostics.py`, `candidates.py`, `sandbox.py` | ✅ | 不直接覆盖生产版本（发布只能经 Manifest 校验的门禁路径） |
| 7. 评测与发布门禁服务 | `evolution/evaluator.py`, `safety.py`, `datasets.py` | ✅ | 独立评测证据替代自评（fail-closed） |
| 8. 版本分发服务 | `release/manager.py`, `release/delivery.py`, `git_flow.py` | ✅/🔹 | 清单/签名/回滚/审计 + 按平台/App 版本/契约/通道/灰度筛选与对账 ✅；制品存储与下载服务未独立 🔹 |

四个受保护控制器：

| 控制器 | 实现 | 状态所有权 |
|---|---|---|
| 运行时控制器 | `core/controller.py` | 任务状态/权限/预算/取消恢复（✅） |
| 进化控制器 | `evolution/controller.py` | 候选与实验（不能自批自发：批准走 API + 审计，✅） |
| 评测门禁控制器 | `ReleaseGate`（`evolution/evaluator.py`） | 独立证据判定（✅） |
| 发布控制器 | `release/manager.py` | 部署/晋级/回滚决策与审计（✅） |

其余条目：

| 补充 B 条目 | 现状 |
|---|---|
| §4.1 任务执行链 / §4.2 进化反馈链 | ✅ 与现有模块一一对应；本地简单任务可在 App 侧由设备模块完成 |
| §7.3 统一关联字段 | ✅ task_id/run_id/correlation_id/causation_id/event_id+幂等键/candidate_id/release_id 全部在事件与运行记录中；runtime manifest 已独立落表（`manifest_<run_id>`） |
| §8 共享基础设施与数据所有权 | 🔹 当前单机 SQLite + 本地制品目录；对象存储/队列/密钥管理的独立部署未做 |
| §9 故障隔离降级表 | ✅ 进化不可用不影响任务、同步断点补传、门禁失败拒绝晋级、健康异常停推广/回滚（monitor）；App 侧健康失败自动回滚 |
| §10 关键架构约束 8 条 | ✅ 已承接（逻辑先行/隔离/可追溯/不自批自发/硬预算/离线自治/兼容可恢复/数据最小化→privacy 过滤） |
| §6 部署边界（3 后端 + 外部模型 + 基础设施） | ⬜ 单进程部署；隔离靠权限与角色（api/auth.py + 审计），物理拆分按文档明确为规模后置 |

## 补充设计 C 映射：《离线日志与可靠同步子系统设计》

该文档与已实现的离线同步链路（`android-app/src/offline/` + `api/events_sync.py`）高度重合，映射基于代码逐项核对：

| 补充 C 条目 | 模块 | 状态 | 说明 |
|---|---|---|---|
| §1 原则 1-3（先落盘再同步/三通道独立/至少一次+幂等去重） | `offline/queue.ts` + `api/events_sync.py` | ✅ | 事件先入持久化队列；同步失败不影响记录；`event_id INSERT OR IGNORE` + `client_requests` 幂等 |
| §1 原则 4 回执持久化语义 | `api/events_sync.py`, `api/server.py` | ✅ | 服务端入库后才返回逐事件 accepted；客户端仅 ack 回执确认的事件（§5.2 第 6 条部分成功明确列出 ✅） |
| §1 原则 5 历史版本不可覆盖 | `task.runtime_manifest` + `runtime_manifests` 表 | ✅ | 服务端每次运行另存独立清单（`GET /tasks/{id}/manifests` 可查）；客户端事件已随批次携带 `runtime_manifest_id` 引用并落库 |
| §1 原则 6 日志是证据不是命令 | `core/privacy.py`, 工具层信任边界 | ✅ | 外部内容不可执行已在工具/导入/回放层承接 |
| §1 原则 7 隐私最小化 | `offline/privacy.ts`, `policy.ts`, `offline/export.ts` | ✅ | sensitive 事件仅本地保留，仅 standard 上传；导出前强制脱敏密钥类字段/值，敏感原文默认不导出 |
| §1 原则 8 日志≠任务恢复 | `core/checkpoint.py` 分离实现 | ✅ | 检查点/副作用账本/恢复策略独立于事件日志（§10 边界表全行 ✅） |
| §3.1 事件字段十项 | `offline/types.ts` OfflineEvent + `api/events_sync.py` | ✅ | 客户端携带 `event_id`/`task_id`/`run_id`/`sequence_no`/`timestamp`/`clock_source=device`/`severity`/`payload`/`privacy_classification`/`runtime_manifest_id`；服务端校验并落库；`attachment_refs` 随事件同步并在 `runtime_events` 持久化（最多 8 项，ID 规则与附件一致） |
| §3.2 本地逻辑表 7 张 | `offline/storage.ts` 单一持久化状态 | ✅ | 运行证据/同步状态/任务状态/隔离区/回执在版本化状态中各自独立结构与生命周期；`sync_receipts` 独立持久化（最近 20 条，按 `receipt_id` 去重，含服务端计数与重放标记）；物理上仍为单文档快照，未按物理表拆分 |
| §4 容量治理 | `offline/queue.ts`, `offline/retention.ts` | ✅ | critical/normal/debug 分级保留（critical 永不自动清理；normal 7 天、debug 1 天）；空间不足按等级腾位并记录损失标记；80% 预警与用户提示；连续 checkpoint 语义保留 |
| §5.1 同步状态机四态 | `offline/queue.ts`, `offline/sync.ts` | ✅ | `pending → in_flight → acknowledged`（确认后删除）；永久拒绝转 `quarantined` 持久保留，可人工重新入队；in-flight 批次持久化，重启后回收 stale 批次；未按独立 `sync_batches` 表拆分 🔹 |
| §5.2 批次与回执协议 8 条 | `offline/apiTransport.ts`, `sync.ts` + `events_sync.py` | ✅ | 稳定 batch_id、事件 ID 不变、部分成功明确、退避抖动、`protocol_version` 1.0、`confirmed`/`unconfirmed` 显式列表、QUARANTINED 隔离均已实现 |
| §5.3 版本关联与版本对账 | `runtime_manifests` + 对账流程 | ✅ | manifests 独立落表可解析（不做最新版覆盖）；对账已在补充 A §7 完成（`installation_id` + 灰度/兼容服务端判定） |
| §6 附件上传与完整性校验 | `api/attachments.py`, `api/server.py`, `core/persistence.py`, `offline/attachments.ts` | ✅/🔹 | 显式授权（`consent` 非 true 直接 403）；元数据+分块 BLOB 落 SQLite，分块 16 KiB–4 MiB、总量 50 MiB、最多 4096 块；`PUT /attachments/{id}/chunks/{n}` 幂等覆盖、`POST .../complete` 拼装全文校验 SHA-256（不一致清空分块并返回 422），完成后才可读；`GET .../content` admin-only 且审计；客户端 `AttachmentService` 计算摘要、256 KiB 分块、持久化进度、续传跳过已收分块，摘要冲突清空进度整体重传。原始字节仅内存驻留，进程重启后需重新选择同一文件（按 SHA-256/大小复用原记录，🔹 非持久化字节） |
| §7 离线诊断包导出 | `offline/export.ts`, `offline/zip.ts`, `offline/sha256.ts`, `offline/nativeExport.ts` | ✅/🔹 | Export Manager 完全离线生成 ZIP：manifest/events/runtime-manifests/task-runs/sync-status/checksums（逐文件 SHA-256 + CRC32，format_version 1.0）；范围/时间/能力/失败过滤；预览敏感类别与警告；密钥类字段/值强制脱敏，敏感原文与原始负载默认关闭；只读不改变同步状态。交付经 Android SAF 文件选择器（`ACTION_CREATE_DOCUMENT`），取消/失败清理半成品；原生构建未在本机验证 🔹 |
| §7.1 包结构 | `offline/export.ts` | ✅ | 必含清单、事件（JSONL）、运行时清单、任务运行、同步状态与校验和；附件默认不带，按白名单/大小/数量过滤 |
| §8 诊断包受控导入 | `api/diagnostics_import.py`, `api/server.py`, `core/persistence.py`, `offline/packageImport.ts`, `offline/nativeImport.ts` | ✅ | 仅解析不执行：包/条目/解压总量上限、压缩比与符号链接拒绝、路径穿越/绝对路径/盘符拒绝、文件与附件后缀白名单、逐文件 SHA-256、`format_version` 校验；事件关系审查（unknown_task / missing_manifest / unknown_manifest / 任务运行不匹配）；按 SHA-256 幂等入隔离区（`diagnostics_imports` + `diagnostics_import_events`），admin-only 且成功/拒绝均审计。客户端经 SAF（`ACTION_OPEN_DOCUMENT`）选择 ZIP，20 MiB 上限与空包在原生/TS 两侧拦截，以 `X-File-Name` + admin Bearer 原样上传；admin token 只在内存使用、不持久化 |
| §9 隐私安全完整性 | `core/privacy.py` + `api/diagnostics_import.py` + `offline/export.ts` | ✅ | 默认结构化事件、传输控制（API 鉴权）、删除/审计、导入不执行包内内容；导出前二次扫描脱敏密钥类字段/值，敏感原文默认关闭 |
| §11 验收标准 11 项 | `android-app/test/offline/` + `tests/test_diagnostics_import.py` | ✅ | 断网记录/续传/幂等/回执一致/乱序(seq)/版本追溯/恢复隔离/永久错误隔离/容量预警与损失标记/恶意导入拒绝/导出完整性与脱敏 ✅；服务端长期不可用：连续 5 次同步失败，或队列有积压且距上次成功 ≥30 分钟时显示告警（成功同步即清除） |
| §12 与总体架构关系 | 补充 B 已映射的群组 | ✅ | 定位一致：同步观测服务 ≠ 门禁 ≠ 版本分发，互不阻塞 |

**说明**：人工诊断通道已形成「离线导出 ↗ 人工传递 ↗ 服务端受控导入 ↺ 在线同步」完整闭环：
导出在客户端完全离线生成带校验和的 ZIP，不改变同步状态；导入既支持服务端 API，也支持客户端
系统文件选择器（admin 凭证仅驻内存），导入只作证据进入隔离区，不触发事件摄取或任何执行，
并按包 SHA-256 幂等。本地回执历史与「服务端长期不可用」告警补齐 §3.2/§11。
补充 C 功能项已全部落地。原生 SAF 交付（导出/导入/附件选择）需真机构建验证；
客户端附件原始字节不落盘，重启后续传需要重新选择同一文件（内容摘要一致时复用原记录与分块进度）。
验收标准 §11 可直接作为该子系统的测试用例来源。

## 服务重启恢复记录

无（UI 集中在 `android-app/App.tsx`；API 使用逐步标准库）。

## 关键设计决策（Arch 22.1 架构决策记录）

| 决策 | 备选 | 理由 |
|---|---|---|
| 运行时同步单线程模型 | asyncio | 任务多为 IO/工具短时执行；简化并发/取消/检查点正确性（Arch 3.2 主动管理并发由 API 层线程承载） |
| SQLite 为主存储 | Postgres | 零依赖、单机部署起步；PRD 91 明确「不要求全部映射为独立物理表」 |
| Mock + OpenAI 兼容双适配器 | 单一实现 | 无 Key 时也能全链路演示与测试，符合 PRD 4.5 解耦 |
| 真实模型凭据回退到 Claude 全局配置 | 复制 Key 到仓库/环境变量 | 避免密钥落盘；显式 `EVOAGENT_API_KEY` 优先，且显式 Key 不复用 Claude 端点，防止错误混配 |
| 评测门禁一票否决安全项 | 加权平均 | Arch 11.5 明确禁止；无安全证据时同样拒绝（fail-closed） |
| 保留集独立于调优数据 | 全部数据一起调优 | PRD 52 / Arch 11.2：保留集只用于门禁决策，候选调优不得见 |
| 高风险工具默认确认（confirm）而非拒绝 | allow/deny | 与 PRD 70/84 一致，默认策略安全；测试可用回调注入 |
| 模型辅助假设必须绑定证据且经探针才能升级 | 直接信任模型解释 | Arch 9.4：模型解释无根因权威；evidence_refs 必须逐字来自快照、置信度封顶 0.5，且无对应探针实验的模型假设不能创建候选 |
| 候选代码用审计钩子 + 环境白名单隔离 | 容器/虚拟机沙箱 | 单机部署下无容器依赖；一次性目录、阻断网络/子进程/沙箱外读写、凭据不继承。定位是受控执行环境而非恶意代码强边界，已在模块文档注明 |
| 幂等由服务端权威判定 | 客户端只去重 | 客户端可能丢状态/多设备并发；`event_id` 主键 `INSERT OR IGNORE` + `client_requests` 表覆盖重试与并发（PRD 73） |
| 离线队列 ack 前不删除，checkpoint 只取连续前缀 | 收到即删 / 跳过空洞 | 断网重试不丢事件；空洞不跳过避免静默漏同步（PRD 72-74） |
| 设备操作只走无障碍节点 + 显式 Intent | 坐标注入/截屏模拟 | 不绕过系统权限边界；结果必须带证据才判 `verified`（PRD 68/84） |
| 日志四层独立表 + 保留期清理 | 单表加标签 | 各层访问角色与保留周期不同（Arch 9.2）；敏感层脱敏后仍不可默认导出 |
| 告警为无状态纯函数评估 | 持久化告警状态机 | 指标快照可重放、评估幂等，避免告警状态与真实指标漂移（Arch 20.2） |
| Prometheus 无样本输出 `NaN` | 省略序列 | 仪表盘/告警规则引用稳定，不因暂无样本而断档 |
| 迁移回归以 `model_override` 固定新旧模型跑同一数据集 | 只看静态兼容性 | 兼容性字段不代表行为等价；固定路由避免回归对比时被默认路由污染（PRD 104/111） |
| 迁移启用默认不切换默认模型 | 启用即全局默认 | 灰度启用与默认切换分离，回滚可精确恢复到启用前状态 |
| 漂移检测只读且默认不发事件 | 每次检测都发事件 | 指标抓取会频繁调用；只有显式 `scan` 才产生 `drift_detected`，避免事件风暴 |
| 漂移报告短缓存（60s） | 每次抓取全量扫描事件窗口 | 观测面高频拉取不应触发重复 SQLite 窗口扫描 |
| 回放只重建、不重执行 | 按事件重放工具调用 | 副作用不可逆；回放用于审计与排障，非状态恢复（Arch 20.3），非 `standard` 负载一律脱敏 |
| 诊断包导出完全本地生成且只读 | 由服务端导出后再下载 | 导出场景本身可能就是断网排障；只读保证不改变/删除待同步证据，逐文件校验和让服务端导入可验证完整性 |
| 导出默认关闭敏感原文与原始负载 | 全量导出更省事 | 诊断包会离开设备；默认最小化，用户显式开启才含敏感原文，密钥类字段/值始终脱敏 |
| 备份用在线备份 API 而非直接拷文件 | 停服复制 `.db` | WAL/并发写入下直接拷贝可能得到不一致快照；恢复同样写入活动连接，减少停服窗口 |
| 恢复前强制安全备份且检查未结束任务 | 直接覆盖 | 误恢复代价高；保留可回退点，避免覆盖运行中任务状态（Arch 20.4） |
| 工具替代只在副作用未发生时可切换 | 失败即换工具 | 已执行/不确定的副作用可能重复发生；同时限制替代工具不得提高副作用等级或权限集合（Arch 6.4） |
| 工作流抽取要求重复支持 + 人工批准 | 单次成功即固化 | 单次成功可能是偶然；候选必须重复出现并通过结构/工具/参数绑定评测，发布前人工批准（PRD 13.4 / Arch 16.2） |
| 更新决策服务端建议 + 客户端独立复核 | 全信服务端 / 全信本地 | 服务端判定兼容性、灰度与目标版本；客户端另校验签名、摘要、交付方式与回滚路径，任一失败拒绝激活（补充 A §5.2） |
| 双槽位引用切换 + last known good | 原地覆盖安装 | 激活只切换引用，失败可原子回滚；原生 APK 必须走平台应用更新，客户端只标记需二进制更新（补充 A §5.3） |
| 候选阶段只同步评测后状态且不参与激活 | 全量下发候选/假设 | 评测前的假设与证据属于服务端内部状态；客户端只看到批准/发布/监测/回滚阶段，避免把候选误当可执行更新（补充 A §2/§8） |

## 迭代待办（下一轮候选）

1. ~~真实 LLM 联调~~（已完成：复用 Claude 全局配置 → opencode zen，Planning / 意图分类 / ReAct 实测通过）
2. ~~Android 真机构建与验收~~（已完成：Gradle 构建通过，模拟器 + 真机安装运行，无障碍点击与离线同步实测）
3. 视觉/OCR 操作、语音入口、后台常驻同步（PRD 64/68）
4. ~~LLM 辅助诊断假设~~（已完成：证据绑定 + 置信度封顶 + 探针验证后才能升级候选）
5. ~~运行时沙箱~~（已完成：`evolution/sandbox.py` + `POST /evolution/sandbox/run` + CLI；网络/子进程/沙箱外读写阻断，凭据不继承）
6. ~~Web 管理端完整界面~~（已完成：90.1-90.7 分区，诊断操作、候选动作、版本/成本/审计回滚、回放/备份/沙箱）
7. 向量检索嵌入（memory 并用关键词 bigram，PRD 25 相关性）
8. ~~**补充 A §5.2/5.3**：App 侧更新管理器~~（已完成：对账-校验-激活-健康检查-回滚-回执，双槽位 + 自动回滚）
9. **补充 A §4/§5.1**：策略包/能力包制品下载与装载（清单字段、分发筛选已完成；实际包内容由 OTA 承载）
10. ~~**补充 A §7**：恢复后版本对账~~（已完成：`installation_id` 对账，灰度与兼容检查在服务端判定）
11. ~~**补充 A §8**：候选状态同步（服务端→App）~~（已完成：`GET /sync/candidates` + `device_candidate_states` + 客户端透明展示；安装实例、部署记录、批次回执此前已落表）
12. ~~**补充 C §7**：离线诊断包导出~~（已完成：纯 TS ZIP + 校验和 + 脱敏 + 预览 + SAF 文件选择器；原生交付待真机验证）
13. ~~**补充 C §3.2/§11**：本地 `sync_receipts` 独立持久化与「服务端长期不可用」用户告警~~（已完成：回执历史最近 20 条按 `receipt_id` 去重；连续失败 5 次或积压 ≥30 分钟告警）；~~客户端导入选择器（IMP-08）~~（已完成：SAF 选择 + admin Bearer 原样上传）
14. ~~**补充 C §6**：诊断附件分块上传~~（已完成：显式授权 + SHA-256 + 分块续传 + 服务端摘要校验 + `attachment_refs` 全链路；原始字节不落盘，重启后需重新选择文件）
