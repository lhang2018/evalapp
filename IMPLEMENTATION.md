# IMPLEMENTATION — PRD / 架构条目 ↔ 代码可追溯性映射

目标（PRD 114）：全量能力保留在统一规划与架构依赖图中；本文档记录每个能力域的实现状态。
状态说明：✅ 本轮可用（含测试）｜🔹 接口已留、实现待迭代｜⬜ 未开始（在迭代计划中）。
逐功能的当前状态、接口、配置与维护约定见 [FEATURES.md](./FEATURES.md)。

基线文档：`EvoAgent自进化智能体产.md`（PRD 1.0）、`EvoAgent自进化智能体系.md`（Architecture 1.0）
及三份补充设计（映射见下方专节）：`EvoAgent_双端协同进化与版本分发架构补充设计.md`、
`EvoAgent_服务端逻辑服务划分与协同机制.md`、`EvoAgent_离线日志与可靠同步子系统设计.md`。
2026-10-21 起新增两包（`docs/`，映射见「全量详细设计正式版 1.0 映射」专节）：
`全量详细设计正式版_1.0`（89 主题 ID，唯一全量目标基线，非 MVP）；
`架构审计与设计汇编包_v0.1` 降级为历史参考、不再作为基线（过渡期审计使命已完成，裁定见专节）。

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
| PRD 64-71 Android | `android-app/` + `android/` | ✅/🔹 | RN 壳 + REST；无障碍节点操作（按文本点击、显式 Intent、节点快照）经 AccessibilityService + DeviceModule；点击/启动以操作前后快照判定页面变化，识别权限弹窗，连续未验证触发安全停止；五态结果与风险闸门；语音输入（系统 SpeechRecognizer，六态、3–30 秒超时、显式授权、不自动提交）与语音反馈（系统 TTS，1200 字上限、可停止）经 `EvoAgentVoice` 模块接入；视觉/OCR 操作待迭代 ⬜ |
| PRD 72-76 离线与双端 | `android-app/src/offline/`; `api/events_sync.py`; `android/.../SyncScheduler.kt`, `SyncWorker.kt`, `HeadlessSyncRunner.kt` | ✅ | 本地任务/事件队列（容量、去重、连续 checkpoint、死信）、隐私过滤、指数退避、断点续传；事件与任务两端幂等；`SUBMITTING` 崩溃恢复需用户对账；WorkManager 后台周期同步（最短 15 分钟，联网 + 电量不低，用户可开关/立即运行），无界面 JS 在任务窗口内直接执行、不依赖前台服务，冷启动实测通过 |
| PRD 77-79 能力包/AI 漫剧 | `workflows/builtin.py` | ✅ | 漫剧 DAG 示例 + 服务适配器可替换（78.4） |
| PRD 80-82 风险分级与发布 | `evolution/controller.py`, `evolution/monitor.py` | ✅ | L0-L4 规则表（81）；发布后监测（82）：时间窗采样、上窗对比、评估任务剔除、安全事件即时回滚、warning hold、`auto_rollback` 可关闭 |
| PRD 83-88 权限/安全/隐私 | `core/permissions.py` | ✅ | 最小权限、高风险确认、外部内容信任边界原则落实在工具层 |
| PRD 89-90 界面 | `api/server.py`, `api/admin.py` + `android-app/App.tsx` | ✅ | 客户端七分区：任务（在线状态/同步统计/本地任务/死信/设备操作/语音输入/结果朗读/系统状态）、离线状态、工作流中心（版本/运行/产物）、记忆中心（过滤/纠正/删除，权限范围 403 与审计）、预算与模型（只读）、权限（无障碍状态、麦克风/语音识别/语音合成可用性、系统设置引导）、更新（版本/清单/兼容性/回滚）；Web 管理端覆盖 90.1-90.7：健康与告警、问题中心与诊断详情（假设/证据/不确定性/探针/模型辅助假设）、候选实验中心、版本中心（发布/模型/迁移/工作流）、成本中心、审计与发布回滚，另有回放/备份/沙箱分区 |
| PRD 91 数据实体 | `core/persistence.py`, `memory/store.py` | ✅ | 逻辑实体的核心子集 |
| PRD 92-99 非功能需求 | 各模块内 | ✅/🔹 | 稳定性→检查点；性能/成本计量在列 |
| PRD 106-113 验收标准 | `tests/`, `android-app/test/` | ✅ | Python 475 项：任务执行/预算/安全/回滚/验收/诊断闭环与趋势/模型辅助假设证据绑定与探针门禁/候选代码隔离执行/规划与意图分类/事件同步幂等与批次回执/诊断附件分块上传与摘要校验/版本对账与应用回执/候选阶段同步/运行清单/计划与预算接线/暂停恢复/鉴权与审计/指标/工具超时重试幂等/工具替代安全链/工作流并行跳过续跑/成功任务抽取与发布门禁/上下文治理与质量检查/评测完整性与 fail-closed 门禁/九级预算与成本报告/十维收益与发布后监测/路由评分/资产库/日志分层与告警/模型迁移执行链/记忆评测/嵌入向量检索/环境漂移/运行回放/备份恢复/管理端分区与安全渲染/发布分支与快进合并/Claude 全局模型配置/任务模型全字段/用户端中心接口/诊断包受控导入；客户端 128 项：离线队列/同步/任务状态/保留等级与损失标记/本地回执与不可用告警/附件分块上传与续传/同内容重新选择复用记录/设备闸门/界面观测与结果验证/语音能力检测与六态/朗读规整与停止/更新管理器与候选阶段同步/诊断包导出与导入/后台同步策略与原生契约 |
| PRD 115 实施顺序 | Phase 1-2 全部 | ✅ | 阶段三（进化闭环）部分完成 |

## PRD 覆盖率盘点（2026-10-20）

按 `FEATURES.md` 功能矩阵统计：**246 项 ✅、3 项待迭代**（视觉/OCR 操作、
端到端加密、多设备冲突合并；另有 1 项明确不做：坐标注入/截屏模拟，不绕过系统权限）。服务端主链路（PRD 7-63、80-88、100-113）已闭环；
剩余差距集中在客户端交互与外部集成，不影响现有运行与安全边界。
三份补充设计引入的目标约束（能力包下发、App 侧安全激活、版本对账、离线可信同步等）映射见下文
「补充设计 A/B/C 映射」三节；版本对账、运行清单、双槽位更新管理器、回执与候选阶段同步已完成；
人工诊断通道（离线导出/双端受控导入/附件分块上传）已闭环，剩余为能力制品装载。

| 剩余缺口 | PRD / Arch | 规模 | 当前状态 | 备注 |
|---|---|---|---|---|
| 视觉/OCR 操作 | PRD 64/68 | L | 仅无障碍节点 + 显式 Intent；语音输入/反馈已实现 | 需设备侧截屏/图像识别；不得绕过系统权限 |
| 端到端加密、多设备冲突合并 | PRD 72-76/86 | L | 无 | 涉及密钥管理与冲突模型 |
| 远端推送、冲突自动合并 | PRD 59-63 | M | 仅本地分支/标签/ff 合并 | 不自动 push；分叉即失败，需人工处理 |

**实现规模参考**：服务端约 17.2k 行 Python（含 CLI），Android 客户端约 9.0k 行 TS/Kotlin，
测试约 9.6k 行；Python 475 项 + 客户端 128 项。

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

## 全量详细设计正式版 1.0 映射：89 主题 ID ↔ 现有实现

**基线裁定（用户决策，2026-10-21）**：
- 《全量详细设计正式版 1.0》（`docs/EvoAgent_全量详细设计正式版_1.0/`）为**唯一全量目标基线**：
  89 主题 + Android 七子系统（A-01..07）+ 八逻辑服务（SV-01..08）+ 四受保护控制器（CTRL-01..04）
  + 14 ADR + 核心接口与数据契约 + 20 条系统级验收。**全量设计推进，不做 MVP 裁剪**。
- 《架构审计与设计汇编包 v0.1》（`docs/EvoAgent_架构审计与设计汇编包_v0.1/`）**降级为历史参考，不再作为基线**：
  它是过渡期审计（完成统一术语/冲突归并/T01-T12 验收使命，结论与现有实现一致），其部署方向表述与正式版不一致处随之作废。

### 总体差距（89 主题逐项核对结论）

> ⚠️ **本节已于 2026-10-21 按源码核验修正（Session 30/33）**。原判定（✅59 / 🔹15 / ⬜15，66%）
> 存在系统性**「接口在、逻辑空」记账偏差**：多处 ✅ 只有字段与签名，没有产生该字段的代码路径。
> 修正方法与逐项证据见 `GAP_ANALYSIS.md`（含文件:行号），本节为结论同步。
> **判定口径自此统一为：✅ 必须同时满足「有实现逻辑 + 有产生该逻辑的代码路径 + 有走通该路径的测试」。**

| 状态 | 数量 | 占比 | 口径 |
|---|---|---|---|
| ✅ 已实现 | 37 | 42% | 有实现逻辑、有产生它的代码路径、有走通该路径的测试 |
| 🔹 部分/接口已留 | 40 | 45% | 主路径可用，但关键分支缺失 / 字段无读取点 / 依赖未接线 |
| ⬜ 未实现 | 12 | 13% | 无对应物，或仅有纯占位 |

> 计数说明：分组表共 84 行，其中 `P-01/02` 合 1 行覆盖 2 主题、`M-01..05` 合 1 行覆盖 5 主题，
> 展开后为 89 主题。上表数字由脚本对分组表逐行统计得出（见文末复核命令）。

**修正明细（18 项由 ✅ 降级）**：
R-03、R-06、R-08、R-10、R-11、R-12、M-01..05、M-08、C-03、C-09、C-11、E-01、E-07、E-12、E-13、O-05、S-03、S-05
（另 O-08 与 M-09 为原 🔹/⬜ 的重新判定，O-08 由 🔹 降为 ⬜，M-09 由原记 ⬜ 上修为 ✅）

其中 **6 项属于"名为实现、实为空壳"，需优先处理**：
- **E-07** 进化候选生成：四类信号在 `evoagent/` 全目录零匹配，全靠手工传参
- **R-12** 副作用账本：无实体、无表、无状态机；检查点字段恒空 → 恢复门禁恒不触发
- **R-11** 恢复游标与恢复前强制安全备份：游标不存在，`grep -c backup controller.py` = 0
- **C-09** 影响分析：9 行实现、零调用方零测试，只查一层直接下游
- **S-05** 执行环节审计：`grep -c audit controller.py` = 0（台账原写"全审计"）
- **R-08** 工具契约成本估算：`grep cost contract.py registry.py` 零命中；`health_check` 死字段

另有 A/SV/CTRL 31 项：✅ 27、🔹 4（SV-04 ContextManifest、SV-08 制品装载，A-02 端侧运行时、A-05 端侧记忆）。

按实施路线（阶段 A-H）视角：

| 阶段 | 内容 | 状态 |
|---|---|---|
| A 可靠执行内核 | Task of Record、状态机、工具契约、Action Guard、受控 ReAct、验收器、检查点、幂等键、**副作用账本** | 🔹 **副作用账本整份缺失**；审批节点生产链路恒放行；人工接管与等待审批未实现（R-03/06/08/10/11/12 降级） |
| B 工作流与能力资产 | DAG、Schema、匹配、注册表、依赖图、生命周期、候选生成 | 🔹 DAG 运行良好；**Capability Asset 实体与生命周期未落地**、影响分析零调用方（C-03/05/09/11 降级） |
| C 记忆、上下文与专家路由 | 分层记忆、ContextManifest、压缩、来源追踪、路由评测、冲突 | 🔹 命名空间未接通（`memory_scope_key` 死字段）；路由无评测集；冲突零检测 |
| D 观测、诊断与独立评测 | 事件、故障分类、工具健康、QEEG、隐藏回归集、失败案例库 | 🔹 **候选无自动信号**（E-07）；失败案例进不了数据集（E-12）；~~工具健康恒真~~（已由 `ae54291` 实装真探针）；进化侧无并发/停止控制 |
| E 主动目标与资源治理 | AGIG Goal Registry、机会、价值排序、自主等级、停止、APRO、预算账本、复盘 | 🔹 Goal注册/十态/五级授权已交付待审；机会、停止、APRO与归因仍待实现 |
| F Android 首发端与离线可靠性 | 交互/审批、原生桥接、事件队列、离线导出、同步回执、版本校验、回滚 | ✅ 高质量（同步幂等/原子回滚/诊断包安全实现扎实）；降级矩阵与端侧缓存缺 |
| G 端云进化闭环与受控发布 | 同步观测、诊断实验、独立门禁、Release Manifest、灰度、回滚 | 🔹 已交付；未发布时 Manifest 组件版本退化为空；制品装载未接 |
| H 领域能力包与跨端扩展 | 编程专家（AST/LSP）、漫剧包、手机操作包、跨端 | 🔹 手机操作 ✅；编程专家无 AST/LSP；漫剧 7 节点全 mock |

### 分组逐项核对（89 主题）

P 产品与部署（8）：

| 主题 | 状态 | 说明 |
|---|---|---|
| P-01/02 产品定位与全量 PRD | ✅ | 定位、边界、硬约束与决策一致；PRD 覆盖见 FEATURES 矩阵。硬约束证据：`ApiAuth`（`evoagent/api/auth.py::ApiAuth`）、`BudgetController`（`evoagent/core/budget.py::BudgetController`）均有测试覆盖|
| P-03 Android 第一终端 | ✅ | 首发端已真机运行（交互/审批/设备操作/离线/更新）；服务端侧证据：`ApiAuth`（`evoagent/api/auth.py::ApiAuth`）、`digest`（`evoagent/api/attachments.py::digest`）|
| P-04 Windows 桌面端 | ⬜ | 设计路线已定，实施后置（跨端复用同一契约） |
| P-05 Mini PC/Linux 宿主 | 🔹 | 服务端（Python 单体）即常驻宿主雏形；未产品化打包与部署文档 |
| P-06 多端与宿主解耦 | ✅ | Android/CLI/Web 分离，UI 不拥有任务权威状态。证据：`get_task`（`evoagent/app.py::get_task`），refs=10/test=7）、`main`（`evoagent/cli.py`，tests 引用 40 次）|
| P-07 本地优先与可选远程 | ✅ | 端侧 Task 状态/事件/检查点本地持久化；远端可降级。证据：`save_task`（`evoagent/core/persistence.py::save_task`）、`EventBus`（`evoagent/core/events.py::EventBus`）|
| P-08 Harness 可替换 | 🔹 | Mock + OpenAI 兼容适配器已隔离；**但无跨 Runtime 的 `AgentRuntimeAdapter` 协议层**（`models/` 下只有两个实现类），真实 DSH PoC 未做（ADR-010 是 14 条中唯一非"已确认"状态） |

R 任务执行内核（12）：

| 主题 | 状态 | 说明 |
|---|---|---|
| R-01 统一入口与 Task of Record | ✅ | 幂等键防重复创建；统一目标/约束/验收/预算接收。证据：`submit_task`（`evoagent/core/controller.py::submit_task`），refs=5/test=7）、`Task`（`evoagent/core/task.py::Task`），refs=50/test=17）|
| R-02 意图理解与多层路由 | ✅ | 意图/领域/复杂度/风险分层路由；低置信回退 general。证据：`classify`（`evoagent/planning/intent.py::classify`），test=6）、`route`（`evoagent/experts/registry.py::route`）|
| R-03 路由质量评测与纠错 | ✅ | **F-53 已落地**（2026-10-22）：`evoagent/evolution/routing_eval.py` 提供路由专用评测集（覆盖全部已注册意图，含歧义用例 + 独立保留集）与评估器，产出**意图准确率/专家准确率/混淆矩阵/top-1·top-3 命中率/兜底率/弃权率/平均置信度**；`regression_cases()` 把失败与低置信样本回流为回归用例（保留集不回流，避免污染验收数据）；`compare()` 做基线对比并判定退化。API：`GET/POST /routing/eval`、`GET /routing/reports`（注册表路由，`server.py` 仅 +1 行 import）。**评测器首次运行即查出两个真实路由缺陷并已修复**：`intent.py` 关键词表含**幽灵意图** `programming`（从未注册，描述表与专家表用 `coding`）→ 无 hint 的代码类任务全部误落 general 专家；`coding` 与 `travel` 原本**无任何关键词** → 这两个领域永远无法被启发式命中。修复后内置集意图与专家准确率均为 100%（修复前 78.95%）。证据：`RoutingEvaluator`（`evoagent/evolution/routing_eval.py::RoutingEvaluator`），test=24）、`builtin_routing_datasets`（`evoagent/evolution/routing_eval.py::builtin_routing_datasets`），test=24；**ALN-07-W（2026-10-23）补工作流匹配与执行模式两维度评测**：`planning/matching.py::WorkflowMatcher` 提供五项检查与模式选择，`WorkflowModeEvaluator` + `builtin_workflow_mode_dataset` 给出执行模式准确率（实测 1.0）；`routing_coverage()` 中 workflow/execution_mode 两层由未评测改为已评测，运行时自动接线仍列为剩余缺口 |
| R-04 Planner 与计划修订 | 🔹 | 目标拆解/依赖校验/验收齐备；**ALN-07-P 已补分级修订原语**（2026-10-22，`evoagent/planning/planner.py::ExecutionPlan.adjust`）：append/replace/replan 分级 + 不变量校验——`completed_work` 单调不减（修订不得丢弃或降级已完成步骤，否则续跑会重放成功副作用）、修订后计划须过结构校验（id 唯一/deps 存在/无环/非空）、违约抛 `PlanRevisionError` 且计划保持修订前状态、修订记录可序列化往返；`evoagent/react/engine.py::_mark_step_failed` 已按 append 级接线。**仍未做**：`_risks_for` 静态风险文案未转为可执行恢复策略；工作流匹配与轨迹编译属 ALN-07 另两项，待拆卡 |
| R-05 ReAct 执行循环 | ✅ | 单动作提议+Schema/权限/预算检查；防循环。证据：`ModelAdapter`（`evoagent/models/base.py::ModelAdapter`）、`MockModel`（`evoagent/models/mock.py::MockModel`），test=2）、`check_tool_call`（`evoagent/core/budget.py::check_tool_call`）|
| R-06 DAG/工作流运行时 | 🔹 | 条件跳过/并发/重试/检查点续跑 4 项真实现；**审批节点在生产链路恒放行**（`workflow_node_confirm` 不在 `evoagent/core/permissions.py:41-44` 的 `high_risk_ops` 名单 → `check_op`（`evoagent/core/permissions.py::check_op`）恒返回 ALLOW），且 `evoagent/workflows/engine.py:80` 使 `WAITING_AUTH` 节点重跑时被永久跳过 → 任务卡死；**补偿是空壳**（`_try_compensate` 空参执行、ctx 未用、异常全吞、只处理失败单节点，内置漫剧的补偿动作 `fs_list` 是只读列目录，物理上不可能回滚） |
| R-07 三种执行模式选择 | ✅ | 稳定工作流优先 → 混合 Agent → 受限动态规划。证据：`WorkflowEngine`（`evoagent/workflows/engine.py::WorkflowEngine`）、`make_plan`（`evoagent/planning/planner.py::make_plan`），test=6）|
| R-08 工具注册表与能力契约 | 🔹 | Schema/权限/超时/幂等齐备；**成本估算 100% 缺失**（契约无任何成本字段）；**`health_check` 死字段已修**（并行工作流 `ae54291`：六工具装真探针——时钟 tick/计算自检/目录可列/可读/建删验写/git --version；`http_get` 显式无探针 → `health()` 返回 `None=unknown` 而非伪装 True；探针抛错 fail-closed 计 False）。**剩余缺口：契约仍无 `cost_model`/`risk_class`/`offline_capable`/`requires_network`/`contract_test_ref` 字段** |
| R-09 Action Guard 动作授权 | ✅ | 授权绑定对象/范围/摘要；高风险默认 confirm。证据：`PermissionController`（`evoagent/core/permissions.py::PermissionController`），refs=5/test=3）、`ToolContract`（`evoagent/tools/contract.py::ToolContract`）。**注：`check_tool`（`:49`）与 `check_op`（`:64`）本身无直接测试调用，测试经 `ToolRegistry` 间接覆盖**|
| R-10 取消、暂停、等待与人工接管 | 🔹 | 取消/暂停/恢复真实现，双 flag 传播正确；**但"等待审批"与"人工接管"未实现** —— `react/engine.py:110` 收到 `request_user` 立即返回并被转成失败 check（任务判 FAILED 而非挂起），`WAITING_USER` 在 `api/server.py` 零命中（无接口提交澄清答案）；工作流任务连暂停都不支持 |
| R-11 检查点与恢复管理 | 🔹 | 恢复前核验副作用的思路正确（`_resume_suspended`（`evoagent/core/controller.py::_resume_suspended`）检查 pending_side_effects 并要求 force）；**但无恢复游标**（`CheckpointManager` 只有 `save`（`evoagent/core/checkpoint.py::save`）/`latest`（`:55`），只能取最新一条）；ReAct 路径检查点不写节点级状态；**"恢复前强制安全备份"完全未接线**（`backup` 符号在 `evoagent/core/controller.py` 零命中 —— 备份能力在 `evoagent/core/backup.py` 有实现，但恢复路径没调用它）。**F-13 已修写入有序性**（先落 paused 检查点再持久化 SUSPENDED），原 50% flaky 已消 |
| R-12 外部副作用账本 | ✅ | 六态持久化账本（`evoagent/core/side_effects.py::SideEffectLedger`，`task_side_effects` 表）：`planned→submitted→confirmed\|failed\|unknown`，unknown 必须先 `reconcile` 才能继续；工具执行前 `begin` 预记账、结果经 `record_from_tool_result` 落态，重复提交/待对账重放被阻断；恢复时 `pending_side_effects` 由账本 `pending` 投影（`evoagent/core/controller.py::_resume_suspended`）而非检查点自持，未知副作用需显式 force。test=12 |

M 记忆上下文（10）：

| 主题 | 状态 | 说明 |
|---|---|---|
| M-01..05 用户/项目/领域/会话/工作记忆 | 🔹 | ALN-05-I已接Task持久绑定、服务主体白名单及真实ReAct读写/恢复；`MemoryStore.search`（`evoagent/memory/store.py::search`）补owner链、去重及冲突分区，保留专家蜂巢。05-M既定范围获第三方接受，05-I新接线待独立复核；完整IAM与端侧缓存另验 |
| M-06 压缩与预算管理 | 🔹 | 共享压缩引擎+预算 ✅（硬约束保留有测试）；但保留是"结构化字段本来就不裁剪"的被动结果，非主动保全逻辑；领域压缩策略未分域 |
| M-07 ContextManifest 来源追踪 | 🔹 | `ContextManifest`（`evoagent/memory/manifest.py::ContextManifest`）已由05-I实际装配并按task/run持久化引用、摘要与压缩前后信息；正文不入清单。独立审阅、端侧失效与隐私删除仍待验收 |
| M-08 记忆冲突与事实更新 | ⬜ | **无冲突识别**。`evoagent/memory/store.py:148` 的 `write` 只做精确去重；`link` **生产代码零调用点**（`evoagent/memory/store.py::link`）（仅 `tests/test_memory_router_diag.py:48` 以 `refines` 调用一次）—— 它本可建立 `contradicts` 关系；无置信合并、无用户确认流 |
| M-09 记忆写入门禁 | ✅ | 实现质量好：无 evidence 的 VERIFIED 写入被强制降级为 PENDING_REVIEW；5 态可信度；未验证经验设 7 天有效期。证据：`write`（`evoagent/memory/store.py::write`），test=26）、`set_status`（`evoagent/memory/store.py::set_status`）。**这是「文档给了明确二元判断→实现就到位」的正面样本**|
| M-10 保留、删除与隐私 | 🔹 | 隐私分级/脱敏 ✅，删除时向量已级联；**但 relations 中指向该 id 的其他记忆留悬空引用**（`related()` 靠 get() 返回 None 静默跳过）；无导出 |

C 专家能力与工作流（14）：

| 主题 | 状态 | 说明 |
|---|---|---|
| C-01 专家注册表 | ✅ | 领域/能力/工具/模型/评测声明齐全。证据：`builtin_experts`（`evoagent/experts/builtin.py::builtin_experts`）、`route`（`evoagent/experts/registry.py::route`），test=2）|
| C-02 受保护共享内核 | ✅ | 安全/权限/预算/门禁不可被专家或候选覆盖（已代码化：L4 禁止普通流程发布、L2/L3 拒绝 actor=="auto"）。证据：`PermissionController`（`evoagent/core/permissions.py::PermissionController`）、`BudgetController`（`evoagent/core/budget.py::BudgetController`），test=14）|
| C-03 领域专家隔离与独立进化 | 🔹 | 5 个专家、独立 system_prompt ✅、tool_whitelist ✅、budget_class ✅；**但"独立评测"是死字段** —— `quality_metrics`（`registry.py:26` 定义、`builtin.py:18/30` 赋值）**零消费点**，无按专家维度跑的评测；`comic_drama` 专家既无 tool_whitelist 也无 quality_metrics，与通用内核完全共用；`mobile_operation` 白名单里无任何设备控制工具 |
| C-04 Tool/Skill/Workflow/Expert 边界 | ✅ | 概念与调用关系与实现一致。证据：`ToolContract`（`evoagent/tools/contract.py::ToolContract`），refs=24/test=7）、`ToolRegistry`（`evoagent/tools/registry.py::ToolRegistry`）、`WorkflowEngine`（`evoagent/workflows/engine.py::WorkflowEngine`）|
| C-05 Capability Pack 能力包 | 🔹 | 清单/下发/状态同步 ✅；**无打包实体** —— `grep "class .*Pack\|Bundle"` 零命中，`bundle_kind` 只是 ReleaseManifest 上的默认字符串，与实际制品内容无关联；制品下载与装载未接线 |
| C-06 轨迹到工作流编译 | ✅ | 重复支持+人工批准+失败注入回归。证据：`extract`（`evoagent/workflows/extraction.py::extract`），test=13）、`publish`（`evoagent/workflows/extraction.py::publish`）|
| C-07 工作流适用性匹配 | ✅ | 输入契约/工具/预算/权限匹配。证据：`node`（`evoagent/workflows/models.py::node`），refs=61/test=2）、`WorkflowEngine`（`evoagent/workflows/engine.py::WorkflowEngine`）|
| C-08 能力资产生命周期 | 🔹 | 候选工作流有状态机；**但 `CapabilityEntry.status`（`evoagent/experts/registry.py:33`）只有 active/experimental/disabled 三值**，与契约 §6 的八态（`evoagent/core/contracts.py:117` 的 `ASSET_LIFECYCLE`：draft/candidate/validating/approved/active/degraded/deprecated/retired）无语义对应 |
| C-09 能力依赖图与影响分析 | 🔹 | `impact_analysis` 已补齐上下游传递闭包与分层回归范围（`evoagent/experts/registry.py::impact_analysis`）：shared 内核变更全量回归、仅领域层受影响走领域回归、无受影响能力则复用证据；被候选创建（`evoagent/evolution/controller.py::_impact_for`）与 `GET /impact`（`evoagent/api/impact_api.py`）真实消费，test=7。**剩余缺口**：依赖图数据仍少（`evoagent/experts/builtin.py` 手工边），`shared` 判定依赖 `domain=="shared"` 约定 |
| C-10 能力去重、别名与合并 | ⬜ | 未实现 |
| C-11 经验来源与晋升证据 | 🔹 | `promote_to_candidate` 的证据门禁是真的（无证据直接 raise、模型假设须先过探针）；**但只覆盖失败证据**（成功运行→进化候选的路径不存在），且**手工入口可零证据建候选**（`evoagent/api/server.py:1141` 接受调用方自述的任意字符串数组，`propose_candidate` 原样存入不校验） |
| C-12 编程专家 AST/LSP | ⬜ | 全仓 grep `AST\|LSP` 在 evoagent 下零命中（仅 node_modules）；`experts/builtin.py` 无编程专家 |
| C-13 AI 漫剧能力包 | 🔹 | 7 节点示例工作流 ✅；**但生成环节全为 mock**（4 个工具都是 lambda 返回 f-string）；无素材资产化、无角色一致性、无 FFmpeg 合成（`mock_compose` 返回字符串不产文件）；`out_path` 形参声明后全函数体未使用，**成片不落盘**；`storyboard` 节点用 `calculate` 工具算 `6` 来"分镜" |
| C-14 受控手机操作能力包 | ✅ | 观察-动作-再观察+权限检查+安全停止（真机验收）；前后节点快照证据完整。证据：`WorkflowEngine`（`evoagent/workflows/engine.py::WorkflowEngine`）、`node`（`evoagent/workflows/models.py::node`）|

E 观测诊断评测与进化（13）：

| 主题 | 状态 | 说明 |
|---|---|---|
| E-01 运行事件与 Trace | 🔹 | task/run/component/version/时延有记录，四层日志独立表齐备；**但无 node 级字段**；**成本/时延/结果全在 `payload` 自由字典**，`events.py:275-279` 靠 `payload.get("latency_s")` 取值，**缺失静默计 0 → 时延聚合被无声低估**，任何依赖它做门禁或收益判定的结论都不可靠 |
| E-02 故障分类与证据等级 | ✅ | 规划/模型/工具/网络/权限/预算/验收分类；证据不足即 unknown。证据：`record_failure`（`evoagent/evolution/diagnostics.py::record_failure`），test=3）、`Hypothesis`（`evoagent/evolution/diagnostics.py::Hypothesis`）|
| E-03 诊断与执行解耦 | ✅ | 诊断只出假设+探针验证，不改生产。证据：`diagnose_task`（`evoagent/evolution/diagnostics.py::diagnose_task`）、`Hypothesis`（`evoagent/evolution/diagnostics.py::Hypothesis`）|
| E-04 工具健康与 Agent 能力分离 | 🔹 | tool_health 探针与质量指标分开（设计正确）；**工具健康探针已实装**（并行工作流 `ae54291`）：六工具真探针 + 三值语义（`True`=健康 / `False`=不健康 / `None`=未声明，未声明不再被算作健康）→ **实际分离度现在可验证**。**剩余缺口：`quality_metrics` 仍零消费点**（C-03），无按专家维度跑的评测 |
| E-05 QEEG 评测治理 | ✅ | 独立评测+基线+保留集；门槛受保护，安全证据缺失时 fail-closed。证据：`judge`（`evoagent/evolution/evaluator.py::judge`），test=8）、`Evaluator`（`evoagent/evolution/evaluator.py::Evaluator`），test=11）|
| E-06 评测集版本/覆盖/防泄漏 | 🔹 | `EvalDataset` 有 version/source/holdout，隐藏保留集独立于调优 ✅；**但 `datasets.py` 全文 47 行仅 4 个用例**，无数据集注册/版本演进机制，无覆盖率台账 |
| E-07 进化候选生成 | ✅ | 四类信号→`register_problem` 登记→`run_signal_pipeline` 消费→`propose_candidate` 自动提案链已落地：`SignalCollector`（`evoagent/evolution/signals.py::SignalCollector`）订阅总线——诊断建案→失败、`task_finalized`→成本异常（近窗中位数判据）、`expert_fallback`→能力缺口、`evolution_monitored`→质量退化；判据/冷却由 `EVOLUTION_SIGNAL_SPEC` 契约化，评测流量不回流（ADR-007）；`run_signal_pipeline`（`evoagent/evolution/controller.py::run_signal_pipeline`）组件级节流后自动提案，是否晋级仍由 evaluator/gate 独立裁决。test=10 |
| E-08 隔离实验环境 | ✅ | `evoagent/evolution/sandbox.py`（网络/子进程/凭据/读写阻断）；容器级隔离明确不做，边界已注明。证据：`run_code`（`evoagent/evolution/sandbox.py::run_code`），test=10）、`SandboxRunner`（`evoagent/evolution/sandbox.py::SandboxRunner`）|
| E-09 发布晋级门禁 | ✅ | 安全一票否决与独立门禁；`deploy_candidate`（`evoagent/evolution/controller.py::deploy_candidate`）核对持久化报告 ID/摘要、候选/评测上下文、批准记录和当前基线/回滚制品，外部 gate_passed=True 不得替代证据。L4/未知风险禁普通发布，L2/L3 需具名批准与发布。直接回归：`tests/test_release_evidence.py`；制品签名与兼容由 `evoagent/release/manager.py::deploy` 复核。真实候选隔离执行/变更制品装载仍待整合，本项局部代码保护不代表整包验收。|
| E-10 稳定/候选轨道隔离 | ✅ | 生产绑定稳定资产；候选不能触碰稳定指针。证据：`build_manifest`（`evoagent/release/manager.py::build_manifest`）、`deploy`（`evoagent/release/manager.py::deploy`）|
| E-11 收益与退化评估 | ✅ | 基线 vs 候选+漂移监测（`evoagent/evolution/drift.py`）；但**若 E-07 无自动候选，该链上游是空的**。证据：`evaluate_benefit`（`evoagent/evolution/benefit.py::evaluate_benefit`），test=12）、`detect`（`evoagent/evolution/drift.py::detect`），test=11）|
| E-12 失败案例转回归资产 | 🔹 | `record_failure` 自动建案例（含 version_manifest + evidence_snapshot，信息量足够）；**但全仓无 DiagnosticCase → EvalCase 转换**（`grep "to_eval\|to_dataset\|regression_case"` 零匹配），案例止步于 diagnostics 表，**进不了 datasets**，"可复现回归资产"不成立 |
| E-13 独立进化控制器 | 🔹 | 预算池隔离 ✅、实验次数上限 ✅；**但无实验并发控制**（进化侧无 Semaphore，任务侧 BoundedSemaphore 不覆盖进化实验）；**无"重复失败即停止"**（`react/engine.py:235` 的 `repeated_failure` 是单任务 ReAct 停止，与进化实验无关，属概念混淆）；无质量/失败率熔断 |

G 自主目标与资源治理（10）：

| 主题 | 状态 | 说明 |
|---|---|---|
| G-01 长期目标注册表 | 🔹 | `GoalStore`（`goals/store.py::GoalStore`）持久化09卷字段、owner/title/版本；Goal CRUD、Task引用与幂等派发已接线；ALN-13-A待独立审阅，机会发现/自动推进不在本卡 |
| G-02 主动机会发现 | ⬜ | 未实现（ADR-013：机会发现只产生候选，不自动成为已授权任务） |
| G-03 目标价值评估与排序 | ⬜ | 未实现。注意 `evolution/benefit.py` 的加权评分属**进化收益**，非目标排序，不可复用 |
| G-04 自主等级与授权矩阵 | 🔹 | `LEVELS`（`goals/store.py::LEVELS`）定义建议/准备/沙箱/执行/长期推进；`check_task`（`goals/store.py::check_task`）将Goal/父链/任务范围交集接到工具与模型预算守卫；生产长期自主推进待ALN-14/15 |
| G-05 Goal 状态机 | 🔹 | `TRANSITIONS`（`goals/store.py::TRANSITIONS`）按09卷十态裁定，CAS版本与命令幂等、期限/撤销/证据守卫、重启持久化；人工推进可用，调度与自动复盘待后续 |
| G-06 无进展检测与停止控制器 | ⬜ | 任务级防循环有（`react/engine.py:157-166`）；**目标级无** → 验收用例 19 永远无法通过 |
| G-07 递归子目标治理 | ⬜ | 仅 1 层 `Task.parent_task_id` + `_build_retry()`；**无深度计数、无数量上限、无环路检测、无重复子目标去重** → 设计要求"不能通过递归生成子目标规避停止"当前无技术阻断 |
| G-08 前台/后台资源调度 | ⬜ | 无 APRO 调度。仅有全局 `BoundedSemaphore(2)`，**无优先级、无前后台区分、无 CPU/磁盘维度**（Android 后台同步属客户端执行通道，不是调度器） |
| G-09 预算账本与费用控制 | 🔹 | 九级预算作用域 + 池化上限 + 预算耗尽拒绝 ✅；**但无执行前预留（无 `reserve()`）、无执行后对账（无 `reconcile()`）、无 CPU/磁盘/并发维度记账**，"未知费用按保守估计"这条设计决策未实现 |
| G-10 主动任务结果归因 | ⬜ | 未实现 |

> **历史设计缺口**：05 卷 G-01..G-10 的可实现契约统计为 **字段名 0 / 接口名 0 / 状态枚举 3 个散文枚举 / 数值阈值 0**（每主题 5 段逐字重复的模板）。唯一可编码来源是 09 卷 §7 Goal 契约（11 字段）。ALN-13现已按09卷补齐[Goal与授权契约](docs/协作开发/设计/ALN-13-Goal与自主授权契约.md)；无进展阈值、自动递归及APRO仍由ALN-14/15补齐，不能据本卡关闭整组。

O 端云协同与可靠性（10）：

| 主题 | 状态 | 说明 |
|---|---|---|
| O-01 本地事件持久化与容量治理 | ✅ | 先落盘+容量预警+损失标记；critical 永不自动清理。证据：`EventBus`（`evoagent/core/events.py::EventBus`），test=4）、`emit`（`evoagent/core/events.py::emit`），test=11）、`ingest`（`evoagent/core/events.py::ingest`）|
| O-02 可靠同步与幂等回执 | ✅ | 至少一次+服务端幂等（`INSERT OR IGNORE` + `client_requests`）+持久化后 ACK+部分成功列出；含 stale in-flight 回收与死信。**实现质量优秀**。证据：`ApiServer`（`evoagent/api/server.py`）、`ingest`（`evoagent/core/events.py::ingest`）|
| O-03 离线诊断包导出 | ✅ | 纯 TS ZIP+逐文件校验和+脱敏；只读不改同步状态。证据：`digest`（`evoagent/api/attachments.py::digest`），test=4）、客户端 `android-app/src/offline/diagnosticsExport.ts`|
| O-04 诊断包受控导入 | ✅ | 隔离区+路径穿越/ZIP 炸弹/压缩比防护+关联审查+按 SHA-256 幂等。**实现质量优秀**。证据：`digest`（`evoagent/api/attachments.py::digest`）、`ApiAuth`（`evoagent/api/auth.py::ApiAuth`）|
| O-05 Runtime Manifest | 🔹 | 不可变清单独立落表 ✅，每次运行落一份 ✅；**但未发布时组件版本退化为空** —— `_version_manifest()` 依赖 `releases.current_release_id`，无发布时返回 `{"version":"unreleased"}`；且缺 `expert_ref`/`router_policy_ref`/`context_policy_ref`/`model_profile_ref`，无法还原"这次运行实际用了哪个专家与路由策略"（削弱 ADR-012 与验收用例 9） |
| O-06 端云协同进化闭环 | ✅ | 证据上行/候选下行/激活回执全链。证据：`record_failure`（`evoagent/evolution/diagnostics.py::record_failure`）、`build_manifest`（`evoagent/release/manager.py::build_manifest`）|
| O-07 版本分发、激活与回滚 | 🔹 | 清单/签名/灰度/双槽位回滚 ✅（**双槽位原子切换实现优秀**）；制品装载未接；`release_secret` 为明文文件，`_sign` 用裸 sha256 而非 HMAC |
| O-08 离线降级与恢复 | ⬜ | **降级矩阵不存在**。`offline/policy.ts:8` 的 `OFFLINE_INTENTS = ["time_query"]` 是硬编码白名单；`degrade`/`degrade_matrix`/`offline_capable` 在客户端仅命中测试断言。O-08 要求"降级必须显式告知，不能伪装成完整云端能力"，当前无声明机制 |
| O-09 附件与大型资产 | 🔹 | 授权+分块+断点续传+SHA-256 校验 ✅（三项能力均真实现）；但大资产保留/清理仅 LRU 截断 20 条，未系统化；原始字节仅内存驻留，重启需重选文件 |
| O-10 多设备状态一致性 | ✅ | 安装实例/部署记录/批次回执/对账/`/sync/candidates`。证据：`list_tasks`（`evoagent/core/persistence.py::list_tasks`），refs=12/test=2）、`save_task`（`evoagent/core/persistence.py::save_task`）|

S 安全治理与工程化（12）：

| 主题 | 状态 | 说明 |
|---|---|---|
| S-01 安全策略与最小权限 | ✅ | fail-closed；专家/候选不能放宽全局规则（已代码化）。证据：`PermissionController`（`evoagent/core/permissions.py::PermissionController`），refs=5/test=3）、`ApiAuth`（`evoagent/api/auth.py::ApiAuth`）|
| S-02 隐私与敏感数据治理 | 🔹 | 脱敏/导出二次扫描 ✅；**但隐私分级口径与契约 §3 不一致** —— 实现为 standard/sensitive/secret/private，契约为 public/internal/sensitive/restricted（数量相同语义不同，跨端有误判风险）；`MemoryItem` 无隐私级别字段 |
| S-03 模型输出与外部内容不可信 | 🔹 | 记忆与执行结果两路有 `_looks_untrusted()` 正则检测（13 条）+ `[不可信数据]` 前缀 ✅；**但不覆盖模型输出本身**，无归一化/编码变体处理（base64/零宽/同形字）、无分级、无审计告警 |
| S-04 本地沙箱与执行隔离 | ✅ | 审计钩子+环境白名单+凭据不继承+超时强杀（容器级隔离明确不做，边界已注明）。证据：`run_code`（`evoagent/evolution/sandbox.py::run_code`），test=10）、`SandboxRunner`（`evoagent/evolution/sandbox.py::SandboxRunner`）|
| S-05 审计日志与可解释性 | 🔹 | API/CLI 边界已审计（`server.py` 约 24 处、`cli.py` 4 处）；**但执行环节零审计** —— `grep -c audit evoagent/core/controller.py` = **0**，所有状态流转、记忆沉淀、失败建案均不留痕。S-05 要求七环节全审计，台账原写"全审计"与代码不符 |
| S-06 ADR 决策治理 | 🔹 | 14 条 ADR 决策台账齐全（`10_术语与架构决策/`）；但无状态流转与 Superseded 关系链 |
| S-07 自动化测试与端到端验收 | 🔹 | **实测 475 Python + 128 客户端**（原记「331 Python + 60 客户端」，三处数字互不一致且均过期，已修正）；含失败注入/重启/恶意输入场景 ✅；**但无独立 E2E/兼容性测试目录**，且存在自证式测试（见 R-12） |
| S-08 模块化单体与技术基线 | ✅ | 与实现一致（SQLite/单进程/模块化单体/零外部依赖）；**F-00 后建表与路由已从共享文件剥离**（`core/schema/` 10 域模块 + `api/routes.py` 注册表）。证据：`all_statements`（`evoagent/core/schema/__init__.py::all_statements`），test=3）、`dispatch_get`（`evoagent/api/routes.py::dispatch_get`），test=50）|
| S-09 运行时可替换性 | ✅ | 业务只依赖自有契约；Mock/OpenAI 兼容双适配器共用同一契约。证据：`MockModel`（`evoagent/models/mock.py::MockModel`），test=2）、`ModelAdapter`（`evoagent/models/base.py::ModelAdapter`）|
| S-10 低资源设备与性能治理 | ⬜ | **完全无**。`BudgetLimits` 只有 token/成本/时长/并发/调用数，无 CPU/内存/磁盘/电量任何硬件资源维度；Android 是第一终端，资源治理缺位直接影响首发可用性 |
| S-11 密钥、配置与凭证 | 🔹 | Key 不落仓库+环境变量+Claude 全局配置复用 ✅；**但无轮换机制、无 KMS 抽象、无密钥扫描器**；`release_secret` 明文文件、`_sign` 裸 sha256 非 HMAC。S-11 要求密钥不得进入提示词/事件/导出包/Git 四处，目前 0 处有技术保障 |
| S-12 备份、迁移与灾难恢复 | 🔹 | 备份/校验/恢复/回放 ✅（在线备份 API、sha256、integrity_check、恢复前安全备份）；**无 schema 迁移机制** —— `FORMAT_VERSION` 只用于**拒绝**不兼容备份，无迁移脚本与升级路径；迁移台账与故障演练未做。**F-00.4 已补迁移框架骨架**（`schema_migrations` 表 + 版本化迁移） |

Android 七子系统（A-01..07）：

| 主题 | 状态 | 说明 |
|---|---|---|
| A-01 交互、任务历史与审批 | 🔹 | UI/审批交互 ✅，Task of Record 一致 ✅；**但审批无持久化记录** —— `approval_id` 全仓零命中，高风险确认是 `confirm_callback -> bool` 的瞬时判定，**无动作摘要绑定、无有效期、无一次性语义、无撤销记录**（A-01 要求的五项绑定全部缺失）；且 `WAITING_USER` 在 `api/server.py` 零命中，用户无法提交澄清答案 |
| A-02 端侧运行时与本地工具桥接 | 🔹 | 设备桥/文件选择/已授权操作 ✅；端侧本地推理运行时未做 |
| A-03 权限与无障碍安全 | ✅ | 最小权限/可见状态校验/不确定即暂停（真机验收） |
| A-04 端侧持久化、事件队列与检查点 | ✅ | 断网/进程杀/重启数据保留（测试覆盖） |
| A-05 端侧记忆、上下文与离线缓存 | 🔹 | 仅任务快照；用户偏好/工作流缓存/能力声明未做 |
| A-06 同步、版本校验、激活与回滚 | ✅ | 双槽位原子切换+签名/兼容校验+健康检查回滚 |
| A-07 端侧诊断、隐私与导出 | ✅ | 积压查看+离线导出+脱敏（原生 SAF 需真机复核） |

八个逻辑服务（SV-01..08）：SV-01/02/03/05/07 ✅（核心运行时、模型层、工具/专家层、events_sync、评测门禁）；
**SV-06 🔹**（诊断/进化服务在，但**候选无自动信号**——E-07 四类信号零实现，服务只能被动接受人工传入）；
SV-04 🔹（05-I服务端身份/ContextManifest已交付review；端侧缓存与实际隐私仓储/删除另验）；SV-08 🔹（制品装载缺）。

四个受保护控制器（CTRL-01..04）：
- **CTRL-01 ✅**（`core/controller.py`）—— 但**执行环节零审计**（见 S-05），可解释性有缺口
- **CTRL-02 🔹**（`evolution/controller.py`）—— 预算池与实验次数上限 ✅，**无并发控制、无重复失败停止**（见 E-13）
- **CTRL-03 🔹**（`evolution/evaluator.py::ReleaseGate`）—— 门禁判定与 fail-closed ✅，但**候选可经手工入口零证据创建**（见 C-11），且测试集仅 4 用例（见 E-06）
- **CTRL-04 ✅**（`release/manager.py`）—— 不可变版本/签名/灰度/回滚；**"评测通过不自动等于发布授权"已代码化**（`deploy_candidate` 检查 L4 禁普通流程、L2/L3 拒绝 `actor=="auto"`）
- ⚠️ **CTRL-03 与 CTRL-04 未真正分离**：评测编排、批准（`approve`）、发布（`deploy_candidate`）三职合一于 `EvolutionController`。ADR-011 要求"候选不能批准自身"，当前靠 `status` 状态机与 `actor=="auto"` 检查间接保证，而非控制器隔离。

### 《核心接口与数据契约正式规范》差异

> 契约字段与枚举的**唯一代码权威**已建于 `evoagent/core/contracts.py`（F-00.3），并记录 6 条文档内部冲突的代码级裁定（`CONFLICT_DECISIONS`）。本节为实现侧对齐状态。

**三份契约整份缺失**：
- **Goal 契约 ⬜** —— 11 字段仅 `goal_id`（可空列）+ `origin` 已落地，无实体、无表、无 CRUD
- **Side-effect Ledger ⬜** —— 十字段零实现，六态枚举零匹配（见 R-12）
- **Capability Asset 🔹** —— 只有 `CapabilityEntry`：`asset_type` 被 `domain` 取代、`lifecycle_state` 只 3 值（契约八态）、`namespace` 完全缺失

**统一错误对象 ✅（F-01 已落地，2026-10-21）**：7 字段（`error_code`/`message`/`retryable`/`correlation_id`/`related_event_id`/`required_action`/`safe_to_repeat`）全部实现于 `evoagent/core/errors.py`（`error_payload`/`normalize_error_payload`/`ApiError`），并在 `evoagent/api/server.py::_json` 单点接入（4xx/5xx 且无显式 `error_code` 时补齐，成功负载原样）。**契约不变量已代码化**：`safe_to_repeat=unknown` 时强制 `required_action=reconcile`（`errors.py:114`），不再只是散文。

其余字段级缺口：
- Task：`acceptance_contract_ref`、`authorization_ref`、`runtime_manifest_id`（实体上）、**Task 级 `idempotency_key`**（键散落三处且不同名：`client_request_id`/`idem_key`/`ToolResult.idempotency_key`）；`created_at` 为 float 而非 RFC3339；`constraints` 为 `List[str]` 而非对象；`status` 缺 `waiting_approval`
- Event：`schema_version`（全仓 py+ts 零匹配）、`payload_ref`（现内联）、`privacy_level` 口径不同、`sync_status` 缺 `acknowledged`
- Tool：`risk_class`（声明式，现为运行时推导，**声明与执行不同源**）、`offline_capable`、`requires_network`、`cost_model`、`contract_test_ref`（契约标 required）
- Runtime Manifest：`runtime_adapter{name,version}`（现字符串）、`model_profile_ref`、`router_policy_ref`、`context_policy_ref`、`expert_ref`、`evaluation_suite_ref`、`environment_digest`（sha256）、`tool_versions[]`、`workflow_ref` 类型
- 统一标识：**`node_run_id` 完全缺失**（DAG 节点无执行实例 ID）、**`approval_id` 完全缺失**（无持久化授权记录）
- 兼容声明（§11 七项）：仅落地 `min_app_version`/`tool_contracts`/`rollback_target`，其中 `compatible_requirements` 是自由字符串而非结构化能力要求；**"缺失声明不得进稳定通道"未做字段级校验**
- 回执协议版本字段 🔹（`protocol_version: "1.0"` 已有）

### 系统级验收用例 20 条

> **本节已按源码核验修正（原记「1-18、20 ✅」与代码不符）**。判定口径同主题表：必须真实可运行，不能靠测试自塞数据。

| 状态 | 用例 | 说明 |
|---|---|---|
| ✅ | 1、3、5、6、7、8、10、11、12、13、15、18、20 | 幂等创建/重启一致/取消/预算/验收门/断网落地/幂等同步/恶意导入隔离/门禁不动指针/健康回滚/压缩保约束/晋级报告。**用例 3 的"可定位"已由 F-01 兑现** —— 错误响应带 `error_code` + `correlation_id` + `safe_to_repeat`（`evoagent/core/errors.py`） |
| 🔹 | 2、9、14、17 | **2 高风险未审批不执行**：工具级真实现，但**工作流审批节点恒放行**（R-06）；**9 MANIFEST 还原**：未发布时组件版本退化为空（O-05）；**14 签名/兼容**：fail-closed 有，但 `_sign` 用裸 sha256 非 HMAC（S-11）；**17 路由回退**：回退逻辑有，但**无路由评测集**，"回退正确"无法度量（R-03） |
| ⬜ | **4**、**16**、**19** | **4 副作用未知先对账**：账本整份缺失，`pending_side_effects` 恒空 → 门禁恒不触发（R-12）；**16 依赖图回归范围**：`impact_analysis` 零调用方（C-09）；**19 主动目标无进展停止**：目标级不存在（G-06） |

**达成真 20/20 的最短路径**：`WP-0.1 → WP-0.2`（用例 4）、`WP-0.8`（用例 16）、`WP-2.0 → WP-2.4`（用例 19）。

### 正式版 1.0 相对原基线（PRD 1.0/ARCH 1.0/三补充）的增量

- 把分散设计收敛为 89 个稳定主题 ID（P/R/M/C/E/G/O/S）+ A/SV/CTRL 结构化卡目，可作为实现索引
- 新增整段设计域：G 组（AGIG 自主目标治理）、M-07/09/10（记忆治理）、C-10/12（去重/AST）、S-10/11/12
- 契约层正式化：10 类数据契约（Task/Event/Tool/Manifest/Asset/Goal/副作用账本/回执/错误对象/兼容声明）
- ADR-001..014 台账与 20 条系统级验收用例（此前分散在各文档）
- 术语表将历史缩写（CLEK/ACMO/ARCBG/APRO/QEEG/KCLG/AGIG）映射到主题 ID（ADR-014）

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
| 后台同步直接在 WorkManager 任务内执行无界面 JS | 每次同步启动前台服务 | Android 12+ 拒绝后台启动前台服务；任务窗口足够完成短同步，前台通知仅作尽力展示。HyperOS 等 ROM 仍可能因自启动权限拒绝冷启动，需用户显式授权 |

## 迭代待办（下一轮候选）

1. ~~真实 LLM 联调~~（已完成：复用 Claude 全局配置 → opencode zen，Planning / 意图分类 / ReAct 实测通过）
2. ~~Android 真机构建与验收~~（已完成：Gradle 构建通过，模拟器 + 真机安装运行，无障碍点击与离线同步实测）
3. 视觉/OCR 操作（PRD 64/68）——语音输入/反馈已完成（系统 SpeechRecognizer/TTS）
4. ~~LLM 辅助诊断假设~~（已完成：证据绑定 + 置信度封顶 + 探针验证后才能升级候选）
5. ~~运行时沙箱~~（已完成：`evolution/sandbox.py` + `POST /evolution/sandbox/run` + CLI；网络/子进程/沙箱外读写阻断，凭据不继承）
6. ~~Web 管理端完整界面~~（已完成：90.1-90.7 分区，诊断操作、候选动作、版本/成本/审计回滚、回放/备份/沙箱）
7. 向量检索嵌入（memory 并用关键词 bigram，PRD 25 相关性）
8. ~~**补充 A §5.2/5.3**：App 侧更新管理器~~（已完成：对账-校验-激活-健康检查-回滚-回执，双槽位 + 自动回滚）
9. ~~**补充 A §4/§5.1**：策略包/能力包制品下载与装载~~（清单字段、分发筛选已完成；制品装载并入待办 17）
10. ~~**补充 A §7**：恢复后版本对账~~（已完成：`installation_id` 对账，灰度与兼容检查在服务端判定）
11. ~~**补充 A §8**：候选状态同步（服务端→App）~~（已完成：`GET /sync/candidates` + `device_candidate_states` + 客户端透明展示；安装实例、部署记录、批次回执此前已落表）
12. ~~**补充 C §7**：离线诊断包导出~~（已完成：纯 TS ZIP + 校验和 + 脱敏 + 预览 + SAF 文件选择器；原生交付待真机验证）
13. ~~**补充 C §3.2/§11**：本地 `sync_receipts` 独立持久化与「服务端长期不可用」用户告警~~（已完成：回执历史最近 20 条按 `receipt_id` 去重；连续失败 5 次或积压 ≥30 分钟告警）；~~客户端导入选择器（IMP-08）~~（已完成：SAF 选择 + admin Bearer 原样上传）
14. ~~**补充 C §6**：诊断附件分块上传~~（已完成：显式授权 + SHA-256 + 分块续传 + 服务端摘要校验 + `attachment_refs` 全链路；原始字节不落盘，重启后需重新选择文件）
15. **阶段 E（Goal基础已交付，持续目标与APRO待补）**：G 组 AGIG 自主目标治理 + APRO——目标注册表（`goal_id`）/机会发现/价值评估/自主等级矩阵/Goal 状态机/无进展停止控制器/子目标治理/前后台调度/结果归因（G-09 预算账本已有九级基础）；
    开工前置 ✅（2026-10-21 已完成）：Task 契约 goal_id/origin 落地（GoalOrigin 枚举 + 持久化列/索引 + controller/API 校验 + 6 项测试）；
    **设计前置 ⬜（未做，阻塞开工）**：05 卷 G 组无字段/接口/枚举/阈值契约，须先补写《G 组自主目标治理补充设计》并裁决 Goal.status 七态 vs 十值冲突 → 见 `ROADMAP.md` WP-2.0 / `FEATURE_SPLIT.md` F-15。
    ALN-13-A：GoalStore已提供实体持久化、十态、五级授权与Task接线，G-01/G-04/G-05记🔹；不以是否恰好存在名为Goal的类作为功能判据。
16. **阶段 C 收尾（M 组）**：M-07 ContextManifest 来源追踪、M-09 记忆写入门禁（候选→确认）、M-10 删除传播、M-08 冲突识别、M-06 压缩策略分域；契约同步补 `run_id`/`schema_version`/`payload_ref` 与统一错误对象
17. **阶段 H（领域能力包与跨端）**：C-10 能力去重与别名合并、C-12 编程专家 AST/LSP、P-04 Windows 桌面端、P-05 Mini PC/Linux 宿主产品化、P-08 真实 Harness PoC；能力包制品下载与装载（原待办 9 并入）
18. **治理与契约收尾**：S-06 ADR 状态/取代链、S-10 低资源与性能调度、S-11 密钥轮换、S-12 迁移台账与故障演练、A-05 端侧记忆/工作流缓存、O-08 宿主不可用降级矩阵、O-09 大资产清理、C-13 素材资产化

> ⚠️ **待办 16-18 之前须插入「T0 正确性修复」批次**（2026-10-21 记账修正新增）。
> 原待办把"补新能力"排在前面，但审计发现 8 处**正在错误运行**的逻辑应先修：
> 副作用账本死字段（R-12，使恢复门禁恒不触发）、工作流审批节点恒放行（R-06）、
> 检查点写入无序（WP-0.1）、执行环节零审计（S-05）、工具成本估算缺失（R-08）、
> 影响分析零调用方（C-09）、记忆命名空间未接通（M-01..05）、候选无自动信号（E-07）。
> 完整排序见 `ROADMAP.md`（43 个 WP / 6 轨道）与 `FEATURE_SPLIT.md`（39 个可并行特性）。

### 记账修正记录（2026-10-21）

| 轮次 | 内容 |
|---|---|
| 初版 | ✅59 / 🔹15 / ⬜15（66%）——存在系统性「接口在、逻辑空」偏差 |
| **第一轮修正** | **✅34 / 🔹36 / ⬜19（38%）**；18 项由 ✅ 降级，2 项重新判定（O-08 降 ⬜、M-09 上修 ✅）；验收用例由「19/20 通过」修正为 **✅13 / 🔹4 / ⬜3**；CTRL-03/04 由「全 ✅」修正为未真正分离 |
| **第二轮修正** | **把「判定依据」本身变成可执行断言**。第一轮只有 🔹/⬜ 行有 `文件:行号` 证据，**33 个 ✅ 行全是散文**——无法复核，等于换个姿势重复同一问题。第二轮为每个 ✅ 行补上可校验符号与行号（44 处引用逐条核对到确切符号），并新增 `tools/verify_claims.py` 把 6 类声明变成机器断言 |
| 配套 | 判定口径提高为三条硬门槛（见 `FEATURES.md` 状态标记）；新增 `FEATURES.md` §9.2「接口在、逻辑空」24 项清单；新增 `tools/verify_claims.py` 台账声明校验器 + `tests/test_verify_claims.py` 回归测试 |
| 证据 | `GAP_ANALYSIS.md`（逐主题文件:行号证据）；**台账声明全部通过机器校验**（`python tools/verify_claims.py --check` → 0 问题） |

#### 第二轮修正查出的自身问题

| 问题 | 发现方式 | 处理 |
|---|---|---|
| 33 个 ✅ 行无机器可验证证据 | 正面证据门禁 | 全部补上符号与行号引用 |
| 4 处引用行号漂移 | 语义引用校验（并行工作流的 F-13 修复改了 `controller.py`） | 全部更正为核实后的行号 |
| 声明绑错主体 | 抽取器把「零调用点」绑到邻近的字典键 | 改写措辞让声明紧贴主体；并新增 `dead_claim_unverifiable` 判据 |
| 表格切分被转义竖线截断 | `E-07` 说明列只剩 113 字，后半段声明永远抽不出来 | 修解析器（`split_row` 识别 `\|`），加回归测试 |
| 校验器自身可能「永远通过」 | 第一版解析返回 0 行却报「全部通过」 | 加 6 项故意构造错误台账的负向测试 |

---

## 状态声明校验（机器可复现，禁止手工估算）

分组表的 ✅/🔹/⬜ 判定与文中每一条证据引用，都必须能被机器复核。
**任何修改本表的提交都必须重跑校验并把实际输出写回「总体差距」小节。**

```bash
python tools/verify_claims.py --check          # 有错退出码 1
python tools/verify_claims.py --check --json   # 机器可读
python tools/verify_claims.py --check --lenient  # 证据不足只报告不判错
python tools/scan_all_ledgers.py              # 全台账行号漂移扫描
python tools/verify_claims.py --check-count    # 额外校验台账声明的测试数与实测一致（较慢）
python tools/check_gap_closure.py             # 缺口闭合：未完成主题是否都落到了特性（2026-10-22 新增）
```

> `scan_all_ledgers.py` 覆盖面更广：扫**全部**顶层 .md 的 `文件:行号` 引用，
> 核对被引行确实含所指符号（`verify_claims.py` 只管 IMPLEMENTATION.md 的主题表）。
> 代码一改引用就可能漂移 —— 这一层专门抓那个。

> `check_gap_closure.py` 管的是**计划层面**的漏：它对本表的每个 🔹/⬜ 主题做
> 「主题 → ROADMAP 工作包 → 特性」三段反查，一个主题若在两条路径中都没被特性认领，
> 就报为**未认领**并以退出码 1 失败。2026-10-22 首次运行即查出 8 个主题在
> ROADMAP 与特性表中双双零覆盖（已补为轨道 T6 / 特性 F-51..F-58）。
> **它的意义**：计划里的漏洞此前只能靠人眼通读发现，现在变成了可执行断言。

### 判定标准（三条硬门槛，缺一不记 ✅）

| # | 门槛 | 机器判据 |
|---|---|---|
| 1 | 有实现逻辑 | 符号存在，且函数体非空 |
| 2 | 有产生它的代码路径 | 该符号在 `evoagent/` 内有**引用**（含字典装配、属性访问、框架回调等间接引用），不是只有定义 |
| 3 | 有走通该路径的测试 | 该符号在 `tests/` 内被**直接调用**，或其所属模块被 `tests/` import |

**不接受「测试自己塞值让分支变绿」**（本次审计发现 R-12 的恢复门禁测试即属此类）。

### 校验器覆盖的 6 类问题

| 类型 | 判据 | 典型场景 |
|---|---|---|
| `citation_path_missing` | 引用的文件不存在 | 路径写错 |
| `semantic_citation_mismatch` | 引用行的内容不含所指符号 | **行号漂移**（代码改了，引用还指旧行） |
| `dead_claim_but_referenced` | 声称「零调用」但符号有引用 | 判定反了 |
| `dead_claim_unverifiable` | 声称「零调用」但主体不是函数/类（字典键、字段名） | 声明绑错了主体，无法验证 |
| `absent_claim_but_present` | 声称「不存在」但模式搜得到 | 判定反了（注意作用域限定） |
| `positive_claim_weak_evidence` | ✅ 行没有可校验符号或引用 | ✅ 判定缺证据支撑 |
| `test_count_drift` | 台账声明的测试数与实测不符 | **并行开发持续加测试，数字必然滞后** —— 让工具报出来，而不是靠人记得更新 |

另校验**状态计数自洽**：总体差距表声明的 ✅/🔹/⬜ 数字必须等于分组表逐行统计结果
（按 89 主题口径展开 `P-01/02`、`M-01..05` 两个合并行）。

### 台账写法约定（让声明可被校验）

1. **声明措辞要紧贴主体**：`link`（`path:374`）**零调用点** —— 
   中间隔了整句话，校验器会绑到邻近的另一个符号上。
2. **缺席声明要带作用域**：`X` 在 `api/server.py` 零命中 ≠ `X` 全仓不存在。
3. **符号与路径之间不要夹中文散文**，否则无法判定二者是「定义关系」。
4. 引用写成 `符号`（`文件:行号`）形式，校验器会核对**该行确实含该符号**。
5. **并行开发中优先用符号锚定形式** `符号`（`文件::符号`）—— 它不依赖行号，
   别人在文件里加几行也不会失效。实测并行工作流的 F-03 改动让
   `controller.py` 的 `submit_task` 从 77 行移到 80 行，
   所有硬编码行号的引用当场失效；锚定形式不受影响。
   （`scan_all_ledgers.py` 与 `verify_claims.py` 两种形式都校验）

### 校验器自身的回归测试

`tests/test_verify_claims.py`（22 项）—— 其中 6 项专门用**故意构造的错误台账**
验证校验器确实会失败（路径错 / 行号漂移 / 符号其实有引用 / 声称缺席却存在 /
状态计数不符 / ✅ 行失去证据）。**没有这些测试，校验器可能退化成「永远通过」
——第一版就犯过这个错：主题行解析返回 0 行，却报「全部通过」。**
