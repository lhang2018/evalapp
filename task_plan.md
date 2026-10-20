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
| 29 | 日志分层与可观测 | 四层日志独立存储/保留/访问（runtime_event 30d、execution_trace 14d、diagnostic_evidence 7d、audit 365d）；证据导出授权；扩展指标（模型/工具/ReAct/恢复/上下文/记忆/进化/回滚/意图维度）；8 条分级告警；`/logs/*`、`/alerts` 与 CLI（PRD 37-40 / 86, Arch 9.2 / 20.1-20.2） | complete（187 项 Python + 29 项客户端） |
| 30 | 模型迁移 + 记忆评测 + 漂移检测 | 模型迁移九步执行链（资产继承/兼容性/同数据集回归/发布组合/灰度启用/回滚）与固定模型覆盖路由；记忆质量评测与运行反馈；环境漂移检测（工具/契约/模型/任务分布/版本变更）接入指标、`environment_drift` 告警、API 与 CLI（PRD 20/28/44/104/111, Arch 7.4/8.2/9.3/9.5） | complete（212 项 Python + 29 项客户端） |
| 31 | 运行回放 + 备份恢复 | 只读任务回放（事件/轨迹/检查点/运行记录重建时间线，敏感负载脱敏，不执行副作用）；三库在线备份打包 + sha256/完整性校验 + 恢复（自动安全备份、拒绝未结束任务）；管理端回放与备份面板；API/CLI（Arch 20.3-20.4, PRD 90） | complete（225 项 Python + 29 项客户端） |
| 32 | 工具替代 + 工作流抽取 | 工具契约 `substitutes` 与安全替代链（仅可重试失败、副作用未发生、禁止副作用/权限/输入升级）；成功任务按意图与工具序列聚合，最小支持度、结构/工具/参数绑定评测、批准/拒绝/发布门禁与审计，发布注册运行时；REST/CLI（Arch 6.4 / 16.2, PRD 13.4） | complete（246 项 Python + 29 项客户端） |
| 33 | 真实模型联调 | `EVOAGENT_PROVIDER=openai` 时从 Claude 全局 `settings.json` 读取 Key/端点/模型（显式环境变量优先、Key 不落盘）；意图分类补充领域描述并注册 `time_query`；非专家 domain 归一化；真实端点完成 Planning/分类/ReAct 实测（PRD 17-20/4.5） | complete（255 项 Python + 29 项客户端） |
| 34 | 模型辅助诊断假设 | 模型仅提出辅助解释：evidence_refs 必须逐字来自证据快照、置信度封顶 0.5、状态固定 proposed；不改案例状态；与规则假设重复跳过；无效 JSON/模型异常降级；未经固定探针实验的模型假设不能升级候选；API/CLI 显式触发（PRD 41-45 / Arch 9.4） | complete（266 项 Python + 29 项客户端） |
| 35 | 候选代码隔离执行 | 一次性沙箱目录、环境白名单（不继承凭据）、审计钩子阻断网络/子进程/动态库与沙箱外读写、超时强杀、输出截断；`POST /evolution/sandbox/run`（admin）+ `sandbox-run` CLI；明确非恶意代码强边界（PRD 47.4 / Arch 18.3） | complete（282 项 Python + 29 项客户端） |
| 36 | Web 管理端完整界面 | PRD 90.1-90.7 分区：运行健康与告警、问题中心与趋势/漂移、诊断详情（假设证据/不确定性/探针/模型辅助假设/升级候选）、候选实验动作、版本中心（发布/模型/迁移/工作流）、成本中心、审计与发布回滚；保留回放/备份/沙箱；动态内容仅 `textContent` 渲染 | complete（284 项 Python + 29 项客户端） |
| 37 | 任务模型全字段 | PRD 7.3 任务解析结果：目标对象、预期输出、复杂度、所需工具/权限、网络依赖、工作流适用性、待确认问题；显式输入优先，规划/工具契约/工作流只补空值；REST 校验（非法枚举/类型 400）与 CLI 参数；回退计划对已知意图附带工具提示 | complete（292 项 Python + 29 项客户端） |
| 38 | 用户端中心页面 | PRD 89.3-89.8：客户端七分区（任务/离线状态/工作流/记忆/预算与模型/权限/更新）；服务端记忆列表/纠正/删除（权限范围 403、审计）、工作流详情（版本/运行/产物）；模型与更新只读展示 | complete（297 项 Python + 29 项客户端） |

## 关键决策（Decisions）
| 决策 | 理由 |
|------|------|
| Python 同步执行模型（非 asyncio） | 可靠、易测试；架构文档不强制并发模型，控制器统一管理并发限制 |
| SQLite 持久化（标准库 sqlite3） | 零依赖、满足任务/检查点/事件/记忆持久化需求 |
| Mock 与 OpenAI 适配器同层 | 适配器契约统一（PRD 4.5 能力与模型解耦） |
| 真实模型凭据复用 Claude 全局配置 | 避免 Key 落仓库/日志；`EVOAGENT_*` 优先，显式 Key 不混用 Claude 端点 |
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
| 日志四层独立表与保留期 | 各层访问角色、隐私等级与保留周期不同（Arch 9.2）；敏感层脱敏后仍不可默认导出 |
| 告警评估无状态 | 指标快照可重放、评估幂等；不维护第二套告警状态，避免与真实指标漂移（Arch 20.2） |
| Prometheus 无样本输出 NaN | 序列与告警规则稳定，不因暂无样本断档 |
| 模型调用计量包装在适配器外层 | 不侵入各模型实现，Mock 与真实模型行为一致 |
| 按任务删日志保留任务实体 | 历史指标/审计可追溯；日志删除本身写审计 |
| 迁移回归固定模型跑同一数据集 | 兼容性字段不代表行为等价；固定 `model_override` 避免默认路由干扰对比结论 |
| 迁移启用与默认模型切换分离 | 灰度启用可回滚到启用前状态，不会因启用动作永久改变全局默认 |
| 漂移检测只读且默认不发事件 | 指标抓取会高频调用；只有显式 `scan` 才发 `drift_detected`，避免事件风暴 |
| 漂移报告 60s 短缓存 | 观测面拉取不应触发重复事件窗口扫描 |
| 记忆收益缺样本标 `insufficient_evidence` | 没有对照样本不能声称记忆带来收益 |
| 回放只重建、不重执行 | 副作用不可逆；回放用于审计/排障，非状态恢复（Arch 20.3）；非 `standard` 负载脱敏 |
| 备份用 SQLite 在线备份 API | WAL/并发写入下直接复制文件可能得到不一致快照；恢复写入活动连接以减少停服窗口 |
| 恢复前强制安全备份并检查未结束任务 | 误恢复代价高；保留可回退点，避免覆盖运行中任务状态（Arch 20.4） |
| 工具替代只在副作用未发生且错误可重试时启用 | 已执行/不确定的副作用重放会造成重复写；替代工具不得提高副作用等级或权限集合（Arch 6.4） |
| 工作流抽取要求重复支持 + 人工批准 + 参数绑定 | 单次成功可能是偶然；候选需结构/工具/绑定评测通过，发布后必须可直接执行（PRD 13.4 / Arch 16.2） |
| 模型辅助假设必须绑定证据且经探针才能升级 | 模型解释易自洽但无根因权威（Arch 9.4）；要求证据引用逐字来自快照、置信度封顶，且无对应探针实验时拒绝升级候选 |
| 候选代码用审计钩子 + 环境白名单隔离 | 单机部署无容器依赖；一次性目录、阻断网络/子进程/沙箱外读写、凭据不继承；定位为受控执行环境，不宣称对恶意代码构成强安全边界（Arch 18.3） |
| 管理端动作复用既有 REST 端点 | 服务端鉴权与审计是最终闸门；前端二次确认只防误触，不承担权限判断（PRD 90 / Arch 18.1） |
| 管理端动态内容只用 textContent/createElement | 服务端数据（症状、证据、模型输出）可能含任意文本；禁止 innerHTML 避免管理端 XSS |

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
| 保留清理测试多删一条轨迹 | 1 | 测试 emit 的 `task_created` 会触发 tracer 落盘；改用非轨迹事件类型 |
| Prometheus 缺少无样本序列 | 1 | `add(None)` 直接跳过；改为输出 `NaN`，保证序列存在 |
| 路由收紧后旧测试假适配器崩溃 | 1 | `resolve()` 要求适配器实现 `profile()`；测试假对象改为实现 `profile()` 并断言 `.profile().model_id` |
| CLI 迁移回滚步骤取值报错 | 1 | `mig.steps` 是列表不是字典；改为遍历查找 `step == "rollback"` |
| 记忆评测收益期望值不符 | 1 | 样本为 4 条有记忆/3 条无记忆，实际收益 0.75；修正断言 |
| CLI 命令返回 JSON 但终端无输出 | 1 | `main()` 忽略返回值；`cmd_status`/`cmd_task --wait` 改为自行打印，其余错误路径统一 `_fail()` |
| 恢复往返测试找不到备份 | 1 | 第二个 App 只查自己的 `backups/`；传入源 App 的绝对备份路径（`_resolve` 允许） |
| 备份 API 测试顺序干扰 | 1 | 409 用例遗留 pending 任务影响后续 404 用例；`finally` 中按 task_id 删除 |
| Windows 临时脚本路径失败 | 1 | `/tmp` 不存在；改用 `tempfile.gettempdir()` 校验管理页 JS |
| 冲突用例假通过 | 1 | PDF/CSV 同时被检索时仍判正确；增加 `expect_absent` 强制冲突失败 |
| 真实模型把“现在几点了”判为 general | 1 | 分类提示只有意图名且 `time_query` 未注册；补充领域描述、注册 `time_query`，复测置信度 0.99 |
| 模型给出未注册 domain（time_query） | 1 | 预算池/记忆作用域按不存在的专家分裂；路由后把 domain 归一化为专家 ID |

## 备注
- 无 LLM Key 时运行 `python -m evoagent.cli demo` 走 Mock 模型全链路。
- 真实模型：设 `EVOAGENT_PROVIDER=openai` 即可复用 Claude 全局配置（Key/端点/模型）；
  也可用 `EVOAGENT_API_KEY` / `EVOAGENT_BASE_URL` / `EVOAGENT_MODEL` 显式覆盖。
