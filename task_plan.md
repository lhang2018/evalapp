# EvoAgent 实现计划

## 目标（Goal）
依据 `EvoAgent自进化智能体产.md`（PRD 1.0）与 `EvoAgent自进化智能体系.md`（Architecture 1.0）实现完整系统：
- **服务端核心（Python）**：真正可运行——统一任务模型、运行时控制器、ReAct、工具契约、检查点、预算、记忆/上下文、工作流、评测、进化候选、发布门禁、可观测性。
- **模型接入**：Mock 适配器（无 Key 可跑通全链路）+ OpenAI 兼容适配器（填 Key 即可用）。
- **Android 客户端（React/JS）**：React Native 工程骨架（含 WebView 化思路，便于后续 OTA 推送），交付源码 + 构建说明。
- **后续迭代**：在本 plan 中追加 Phase 处理优化项。

## 状态（Status）：in_progress

## Phases
| # | Phase | 内容 | Status |
|---|-------|------|--------|
| 1 | 工程基建 | 目录结构、config、planning 文件 | complete |
| 2 | 核心内核 | task.py / state_machine.py / budget.py / permissions.py / events.py / checkpoint.py / persistence | complete |
| 3 | 运行时 | controller.py（含恢复/取消/验收编排） | complete |
| 4 | 模型层 | base/mock/openai_compat/router + 模型注册表 | complete |
| 5 | 工具系统 | contract/registry+builtin 工具（含副作用等级、幂等键） | complete |
| 6 | ReAct | engine.py 循环 + 防循环机制 | complete |
| 7 | 规划 | planner.py + 规划验收 | complete |
| 8 | 记忆与上下文 | memory/store.py + memory/context.py | complete |
| 9 | 专家体系 | experts/ 注册表 + 首批专家配置 | complete |
| 10 | 工作流 | workflows/ DAG 引擎 + 漫剧示例流程 | complete |
| 11 | 验收与评测 | acceptance/verify.py + evolution/evaluator.py（评测集+门禁） | complete |
| 12 | 进化系统 | evolution/candidates.py + controller.py 候选生命周期 | complete |
| 13 | 发布管理 | release/manager.py（清单/门禁/签名/回滚） | complete |
| 14 | REST API | api/server.py（任务提交/查询/事件，供 Android 用） | complete |
| 15 | CLI + 演示 | cli.py + examples/demo.md | complete |
| 16 | 测试 | pytest：状态机/预算/ReAct/检查点恢复/工具契约/记忆/工作流/门禁/API | complete（51 passed） |
| 17 | Android 工程 | React Native 骨架 + API 客户端 + 构建说明 | complete |
| 18 | 可追溯性文档 | README + IMPLEMENTATION.md（PRD 条目↔代码映射） | complete |
| 19 | Git 基线 | `.gitignore` + `main` 基线提交 | complete |
| 20 | 诊断闭环 | 诊断案例/多假设规则/固定探针/持久化/失败钩子/候选升级/API/CLI（PRD 41-45） | complete |
| 21 | 真实模型规划 | Planning JSON 校验与回退 + 意图分类器（PRD 7-8/11） | complete |
| 22a | 离线队列与幂等同步 | 客户端本地任务/事件队列、隐私过滤、退避重试、死信；服务端事件摄取与任务幂等（PRD 72-76） | complete |
| 22b | Android 无障碍 | Manifest/原生壳修复、AccessibilityService 节点操作、DeviceModule、风险闸门与五态结果（PRD 64-71） | complete（构建 + 模拟器/真机验证） |
| 22c | 迭代优化其余 | 真实 Key 联调、Android 真机构建验收已完成；视觉/语音、候选代码沙箱、Web 管理端仍待迭代 | partial |
| 23 | 执行内核接线 | 计划驱动 ReAct + 进度持久化、模型调用计量与硬限额、路由接入运行路径、暂停/恢复（累计预算 + 副作用闸门）、API/CLI（PRD 11-12/19/34-36/55-56） | complete（92 项 Python 测试） |
| 24 | 全面补强 | 控制面（API 鉴权/审计/指标/管理页）、工具超时+重试安全规则+输出契约+幂等日志+只读 Git、工作流并行/跳过/超时/续跑+资产库、预算报表、记忆关系/混合检索/使用统计、质量优先路由评分、诊断趋势/故障注入、客户端系统状态面板（PRD 14-20/21-28/37-45/55-58/89-90） | complete（123 项 Python + 27 项客户端） |
| 25 | 上下文治理与评测完整性 | 装配优先级/动态预算/结构化压缩/质量检查（Arch 8.5-8.7）；独立保留集/基线等价/确定性安全套件/安全证据 fail-closed 门禁（PRD 51-54, Arch 11） | complete（135 项 Python + 29 项客户端） |
| 26 | 九级预算与成本报告 | 单任务/单次模型调用/单工具/工作流作用域/候选实验/进化日与月/提供方/专家池 + 并发上限；按任务/领域/模型/版本/实验的成本报告（PRD 55-58, Arch 12）；`GET /costs` + `cli costs` | complete（149 项 Python + 29 项客户端） |
| 27 | 全量功能记录文档 | `FEATURES.md`：按领域记录已实现/部分/未实现功能、代码位置、API/CLI/配置/数据表/测试矩阵与维护流程；README/IMPLEMENTATION 增加入口 | complete |
| 28 | 十维收益与发布后监测 | 十维收益向量（质量/成功率/稳定性/恢复/成本/时延/安全/维护复杂度/跨领域/长期）；跨领域回归门禁；发布后时间窗采样、上窗对比、评估任务剔除、安全即时回滚、promote/hold/rollback 判定（PRD 50 / 82, Arch 11.5 / 12.3） | complete（170 项 Python + 29 项客户端） |

## 关键决策（Decisions）
| 决策 | 理由 |
|------|------|
| Python 同步执行模型（非 asyncio） | 可靠、易测试；架构文档不强制并发模型，控制器统一管理并发限制 |
| SQLite 持久化（标准库 sqlite3） | 零依赖、满足任务/检查点/事件/记忆持久化需求 |
| Mock 与 OpenAI 适配器同层 | 适配器契约统一（PRD 4.5 能力与模型解耦） |
| 影子执行的高风险工具 | PRD“计划完成≠实际完成”，file_write 等高风险工具需 confirm 回调 |
| Android 选用 React Native | 用户要求 React/JS + 后续 OTA 直接推送（Expo Updates/CodePush 路线） |
| 诊断规则优先、模型不判根因 | Arch 9.4：模型解释不可当真实根因；假设由确定性证据规则生成，置信度是评分 |
| 诊断实验固定探针 + 硬限额 | 与生产隔离、无外部网络、每案/每日受限（PRD 43/44） |
| 诊断不发布改动 | 假设只能显式升级为候选，发布仍走评测门禁与 L0-L4 规则（PRD 45/81） |
| 幂等由服务端权威判定 | 客户端状态可能丢失或多设备并发；事件 `INSERT OR IGNORE` + `client_requests` 表保证重试安全 |
| 离线队列 ack 前不删除 | 断点续传不丢事件；checkpoint 只推进连续前缀，避免空洞被跳过（PRD 72-74） |
| 设备操作仅无障碍节点 + 显式 Intent | 不绕过系统权限边界；无证据不判 verified，高风险需独立确认（PRD 68/84） |
| 安全证据缺失时门禁 fail-closed | 缺少证据不等于通过；证据由确定性安全套件产出，评测器不自行声明（Arch 11.5） |
| 保留集只用于门禁决策 | 与调优数据隔离，避免过拟合后发布（PRD 52 / Arch 11.2） |
| 候选实验预算双记账 | 用量同时记入 `candidate:<id>` 与 `evolution`，既限制单次实验又不绕过日/月池 |
| 评测成本取任务自身快照 | 并发下全局账本差值会混入其他任务；任务 `result_evidence.budget.usage` 才是准确归因 |
| 候选预算耗尽按用例失败处理 | 剩余用例记失败并让门禁拒绝，而不是抛异常中断整批评测 |
| 收益维度缺证据标 `insufficient_evidence` | 没有数据不等于改善；长期收益需 ≥20 条发布后样本，稳定性用失败证据代理 |
| 发布后监测排除评测任务 | `source=evaluation` 的任务是自我验证，计入监测会虚增成功率 |
| 安全事件优先于样本门槛 | 安全事件即使样本不足也立即回滚，不被 `min_samples` 延迟 |
| 自动回滚可关闭但保留建议 | 高风险场景允许人工决策；关闭时输出 `rollback_recommended` 而非静默放行 |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| pytest 未安装 | 1 | 保留 tests/，用 unittest 编写，pytest 兼容运行也可用 |
| 测试间端口冲突/挂起 | 1 | `ApiServer.shutdown()` 未关闭监听套接字，Windows SO_REUSEADDR 让新实例 bind 成功但不服务；shutdown 与 finally 都调用 `server_close()` |
| Node 24 无法解析参数属性 | 1 | type-stripping 不支持 constructor parameter properties；所有 TS 类改为显式字段 + 赋值 |
| 队列 checkpoint 全确认后少 1 | 1 | 无待传事件时 checkpoint 取 `next_seq`，而非 `next_seq - 1` |
| 被拒事件同一轮重复重试 | 1 | 处理含 rejected 的批次后立即结束本轮 flush |
| 动态预算测试两条上下文长度相同 | 1 | 800 字符记忆超出 35% 记忆份额，宽/窄预算都被整体丢弃；改用可落入宽预算的记忆验证动态差异 |
| 评测成本差值恒为 0 | 1 | 执行器用 evolution scope 的前后差值统计，而任务用量记在 runtime scope；改为读取任务自身预算快照 |
| 成本报告缺少候选元数据 | 1 | `list_eval_reports` 只返回 data；改为返回 report_id/dataset_id/candidate_id/created_at/data |
| 监测测试辅助重放已过的状态 | 1 | `to_monitoring` 从 `TRIAGED` 开始与 `CANDIDATE_CREATED` 冲突；改为从 `TESTING` 开始 |
| warning-hold 用例触发回滚 | 1 | 错误率 0.20 超过 0.15 阈值；改用手工指标（成功率 0.90、错误率 0.05、成本比 1.35） |

## 备注
- 无 LLM Key 时运行 `python -m evoagent.cli demo` 走 Mock 模型全链路。
- 有 Key 时设 `EVOAGENT_PROVIDER=openai` 与 `EVOAGENT_API_KEY`。
