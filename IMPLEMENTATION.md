# IMPLEMENTATION — PRD / 架构条目 ↔ 代码可追溯性映射

目标（PRD 114）：全量能力保留在统一规划与架构依赖图中；本文档记录每个能力域的实现状态。
状态说明：✅ 本轮可用（含测试）｜🔹 接口已留、实现待迭代｜⬜ 未开始（在迭代计划中）。
逐功能的当前状态、接口、配置与维护约定见 [FEATURES.md](./FEATURES.md)。

## 服务端核心（Python, `evoagent/`）

| PRD / Arch 条目 | 模块 | 状态 | 说明 |
|---|---|---|---|
| PRD 7 统一任务入口 / Arch 3.1 任务模型 | `core/task.py`, `core/controller.py` | ✅ | 统一任务实体、分类→规划→执行→验收 |
| PRD 8 意图识别与路由 | `experts/registry.py` route(); `planning/intent.py` | ✅ | 显式 hint 优先；模型 JSON 分类限定已注册意图；失败回退关键字启发式；低置信 + 高风险在控制器暂停 |
| PRD 9-10 领域专家/能力依赖图 | `experts/` | ✅ | 注册表 + 5 专家 + 依赖边 + impact_analysis |
| PRD 11-13 Planning/ReAct/工作流 | `planning/`, `react/`, `workflows/` | ✅ | 模型 JSON 计划（≤12 步/唯一 id/依赖存在/无环）失败可追溯回退模板；计划注入 ReAct 并持久化进度（done/failed/pending）；失败步回退调整保留原目标与硬约束；防循环（12.3）、固化工件（13.4 有验收门禁）；工作流 DAG 按依赖分层并行、条件跳过、节点超时、逐批检查点与续跑（复用已完成输出）；资产库序列化/加载 |
| PRD 14-16 工具生命周期/契约/恢复 | `tools/` | ✅ | 契约校验、五层结果区分（15）、错误分类；真实超时（工作线程）；仅无副作用或带幂等键的工具重试；输出契约校验；幂等执行日志持久化；只读 Git 适配器（白名单子命令，禁 shell/网络） |
| PRD 17-20 Harness/模型管理 | `react/engine.py`, `models/` | ✅ | 适配器契约统一；模型档案/路由/迁移步骤表；运行时按 intent/domain 路由，选择结果写入 `runtime_manifest.model_route`；质量优先加权评分（质量 0.70 / 成本 0.15 / 时延 0.15）与 `rank()` 决策解释，显式路由与可用性过滤优先 |
| PRD 21-28 记忆系统 | `memory/store.py` | ✅ | 类型/归属/可信状态/写入流程/冲突替代/过期；记忆关系（refines 等）持久化与查询；词法 + 局部敏感哈希向量混合相关性；使用计数与 kind/scope/status 统计 |
| PRD 29-33 上下文治理 | `memory/context.py` | ✅ | 装配优先级（约束/状态/验收永不裁剪）、动态预算（模型 context_limit 约束）、结构化压缩 + 自然语言摘要、质量检查（约束保留/跨域污染/不可信指令/溢出/状态缺失）；装配统计并回写记忆使用计数 |
| PRD 34-36 检查点/恢复/取消 | `core/checkpoint.py`, `core/controller.py` | ✅ | 恢复先验证副作用状态（35），未知副作用需显式 force；暂停/恢复共享累计预算；FAILED 恢复生成带 `parent_task_id` 的新任务；取消阻止后续动作 |
| PRD 37-40 运行日志/可观测 | `core/events.py`, `core/metrics.py`, `api/server.py` | ✅ | 事件模型字段与 Arch 9.1 对齐；旧库自动补列；客户端事件批量幂等摄取（`POST /events/sync`）；`GET /metrics`（JSON + Prometheus）、`GET /audit`、`GET /admin`；进化/发布变更写审计。日志分层待细化 🔹 |
| PRD 41-45 诊断/故障分类 | `evolution/diagnostics.py`, `evolution/probes.py` | ✅ | 失败自动沉淀持久化案例；规则从证据生成 A/B/C/D 多假设（支持/反对证据+不确定性）；6 个固定探针有界无外网，支持 `error/timeout/fail` 故障注入；趋势聚合（按状态/意图/类别/组件）；假设显式升级候选。LLM 辅助假设生成待迭代 🔹 |
| PRD 46-50 自进化引擎 | `evolution/` | ✅ | 候选生命周期状态机与 Arch 10.4 完全一致；收益十维（50）中质量/成本/时延可用 |
| PRD 51-54 评测体系/门禁 | `evolution/evaluator.py`, `evolution/safety.py` | ✅ | 五层架构中第 3/4 层实现；数据集元数据/版本/适用范围与独立保留集（PRD 52）；基线数据集/版本不等价直接拒绝；安全证据由确定性套件产出，缺失时门禁 fail-closed（Arch 11.5）；覆盖硬难度回归、约束违规、工具失败率、恢复成功率、成本/时延 |
| PRD 55-58 成本控制 | `core/budget.py` | ✅ | 九级硬预算：单任务（模型/工具/成本/时长/并发）、单次模型调用成本、单工具（次数/成本，可覆盖）、工作流作用域、候选实验、进化日/月池、模型提供方池、领域专家池；规划与 ReAct 共用同一预算并逐次计量，超限 FAILED；成本报告按任务/领域/模型/版本/候选实验（成本增量、质量增益、失败浪费），经 `GET /costs` 与 `cli costs` 暴露 |
| PRD 59-63 Git/版本/发布 | `release/manager.py`; git 基线 | ✅ | 清单、签名、验证、灰度接口、回滚；已建立 git 基线（main），自动分支/合并操作 ⬜ |
| PRD 64-71 Android | `android-app/` + `android/` | ✅/🔹 | RN 壳 + REST；无障碍节点操作（按文本点击、显式 Intent）经 AccessibilityService + DeviceModule，五态结果与风险闸门；视觉/OCR/语音操作待迭代 ⬜ |
| PRD 72-76 离线与双端 | `android-app/src/offline/`; `api/events_sync.py` | ✅ | 本地任务/事件队列（容量、去重、连续 checkpoint、死信）、隐私过滤、指数退避、断点续传；事件与任务两端幂等；`SUBMITTING` 崩溃恢复需用户对账 |
| PRD 77-79 能力包/AI 漫剧 | `workflows/builtin.py` | ✅ | 漫剧 DAG 示例 + 服务适配器可替换（78.4） |
| PRD 80-82 风险分级与发布 | `evolution/controller.py` | ✅ | L0-L4 规则表（81）；发布后监测待接指标源 🔹 |
| PRD 83-88 权限/安全/隐私 | `core/permissions.py` | ✅ | 最小权限、高风险确认、外部内容信任边界原则落实在工具层 |
| PRD 89-90 界面 | `api/server.py` + `android-app/App.tsx` | 🔹 | 客户端任务中心：在线状态/同步统计/本地任务/死信/设备操作面板/只读系统状态（任务指标、预算、发布、进化候选）；管理端（90.1-90.7）⬜ |
| PRD 91 数据实体 | `core/persistence.py`, `memory/store.py` | ✅ | 逻辑实体的核心子集 |
| PRD 92-99 非功能需求 | 各模块内 | ✅/🔹 | 稳定性→检查点；性能/成本计量在列 |
| PRD 106-113 验收标准 | `tests/`, `android-app/test/` | ✅ | Python 149 项：任务执行/预算/安全/回滚/验收/诊断闭环与趋势/规划与意图分类/事件同步幂等/计划与预算接线/暂停恢复/鉴权与审计/指标/工具超时重试幂等/工作流并行跳过续跑/上下文治理与质量检查/评测完整性与 fail-closed 门禁/九级预算与成本报告/路由评分/资产库；客户端 29 项：离线队列/同步/任务状态/设备闸门 |
| PRD 115 实施顺序 | Phase 1-2 全部 | ✅ | 阶段三（进化闭环）部分完成 |

## 服务重启恢复记录

无（UI 集中在 `android-app/App.tsx`；API 使用逐步标准库）。

## 关键设计决策（Arch 22.1 架构决策记录）

| 决策 | 备选 | 理由 |
|---|---|---|
| 运行时同步单线程模型 | asyncio | 任务多为 IO/工具短时执行；简化并发/取消/检查点正确性（Arch 3.2 主动管理并发由 API 层线程承载） |
| SQLite 为主存储 | Postgres | 零依赖、单机部署起步；PRD 91 明确「不要求全部映射为独立物理表」 |
| Mock + OpenAI 兼容双适配器 | 单一实现 | 无 Key 时也能全链路演示与测试，符合 PRD 4.5 解耦 |
| 评测门禁一票否决安全项 | 加权平均 | Arch 11.5 明确禁止；无安全证据时同样拒绝（fail-closed） |
| 保留集独立于调优数据 | 全部数据一起调优 | PRD 52 / Arch 11.2：保留集只用于门禁决策，候选调优不得见 |
| 高风险工具默认确认（confirm）而非拒绝 | allow/deny | 与 PRD 70/84 一致，默认策略安全；测试可用回调注入 |
| 幂等由服务端权威判定 | 客户端只去重 | 客户端可能丢状态/多设备并发；`event_id` 主键 `INSERT OR IGNORE` + `client_requests` 表覆盖重试与并发（PRD 73） |
| 离线队列 ack 前不删除，checkpoint 只取连续前缀 | 收到即删 / 跳过空洞 | 断网重试不丢事件；空洞不跳过避免静默漏同步（PRD 72-74） |
| 设备操作只走无障碍节点 + 显式 Intent | 坐标注入/截屏模拟 | 不绕过系统权限边界；结果必须带证据才判 `verified`（PRD 68/84） |

## 迭代待办（下一轮候选）

1. 真实 LLM 联调（openai_compat/规划/分类已接通，需 Key 验证）
2. ~~Android 真机构建与验收~~（已完成：Gradle 构建通过，模拟器 + 真机安装运行，无障碍点击与离线同步实测）
3. 视觉/OCR 操作、语音入口、后台常驻同步（PRD 64/68）
4. LLM 辅助诊断假设（当前规则+探针已闭环；模型不参与根因判定，PRD Arch 9.4）
5. 运行时沙箱（候选真实代码执行隔离，PRD 47.4/L3）
6. Web 管理端（PRD 90） — API 数据层已备
7. 向量检索嵌入（memory 并用关键词 bigram，PRD 25 相关性）
