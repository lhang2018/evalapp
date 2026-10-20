# EvoAgent 全量功能记录

本文件是项目的**功能总账**：记录当前已实现、部分实现与未实现的能力，给出代码位置、接口、配置、测试与已知限制。
`task_plan.md` 记录「计划与阶段」，`progress.md` 记录「每轮会话日志」，本文件记录「功能当前状态」——三者互补。

> 维护约定见文末 [维护流程](#维护流程)。**后续任何功能开发，完成后必须在本文件对应功能表更新状态，并在[变更记录](#变更记录)追加一条。**

## 状态标记

| 标记 | 含义 |
|---|---|
| ✅ | 已实现，有自动化测试或端到端验证 |
| 🔹 | 接口/骨架已就位，实现待迭代 |
| ⬜ | 未开始（已列入迭代待办） |

测试基线（2026-10-20）：**Python 255 项 + 客户端 29 项全部通过**。

---

## 1. 系统概览

```
Android 客户端 (React Native / TypeScript)
   ├─ 在线：REST → 服务端
   ├─ 离线：本地任务/事件队列 → 重连幂等重放
   └─ 设备：AccessibilityService 节点级操作（风险闸门）
                    │
                    ▼
Python 服务端（标准库，SQLite）
   ├─ 稳定运行轨道：API/CLI → RuntimeController → Planning / ReAct / Workflow / 验收
   │                  模型路由（Mock / OpenAI 兼容）· 工具注册表 · 九级预算 · 记忆与上下文
   └─ 独立进化轨道：运行事件 → 诊断案例 → 进化候选 → 评测门禁 → ReleaseManager
```

- 服务端目录：`evoagent/`；测试：`tests/`。
- 客户端目录：`android-app/`（TS 核心 + `android/` Kotlin 原生壳）；测试：`android-app/test/`。
- 可追溯性映射：`IMPLEMENTATION.md`；阶段计划：`task_plan.md`；会话日志：`progress.md`。

---

## 2. 服务端功能清单

### 2.1 任务与运行时（PRD 7 / 34-36 / Arch 3）

| ID | 功能 | 状态 | 代码位置 | 说明 / 限制 |
|---|---|---|---|---|
| RT-01 | 统一任务模型（目标、意图、领域、风险、状态、预算、证据） | ✅ | `core/task.py` | 分类→规划→执行→验收统一入口 |
| RT-02 | 任务状态机（含终态不可变） | ✅ | `core/state_machine.py` | 非法迁移拒绝 |
| RT-03 | 运行时控制器编排 | ✅ | `core/controller.py` | 分类、路由、规划、ReAct、验收、收尾 |
| RT-04 | 检查点保存 / 恢复 | ✅ | `core/checkpoint.py`, `core/controller.py` | 恢复前校验副作用状态 |
| RT-05 | 暂停 / 恢复 | ✅ | `core/controller.py`, `api/server.py`, `cli.py` | 暂停后不再发起新模型调用/工具；共享累计预算 |
| RT-06 | 取消 | ✅ | `core/controller.py`, `api/server.py` | 阻止后续动作 |
| RT-07 | FAILED 重跑为新任务 | ✅ | `core/controller.py` | 新 `task_id` + `parent_task_id` + `resumed_from`，不改写历史 |
| RT-08 | 未知副作用恢复需显式 force | ✅ | `core/controller.py` | 存在 `pending_side_effects(state=unknown)` 时抛 `PermissionError` |
| RT-09 | 并发上限 | ✅ | `config.py`（`max_concurrent_tasks`） | API 层线程承载 |
| RT-10 | 结果证据与预算快照 | ✅ | `core/task.py`, `core/controller.py` | `result_evidence[-1].budget.usage` 是成本归因权威来源 |

### 2.2 意图识别与规划（PRD 8 / 11-12）

| ID | 功能 | 状态 | 代码位置 | 说明 / 限制 |
|---|---|---|---|---|
| PL-01 | 意图分类：显式 hint 优先 | ✅ | `planning/intent.py` | hint 匹配已注册意图直接短路 |
| PL-02 | 模型 JSON 意图分类 | ✅ | `planning/intent.py` | 结果限定已注册意图；失败回退 |
| PL-03 | 关键字启发式回退 | ✅ | `planning/intent.py` | 低置信标记，高风险时控制器暂停 |
| PL-04 | 模型 JSON 计划 + 校验 | ✅ | `planning/planner.py` | ≤12 步、唯一 id、依赖存在、无环 |
| PL-05 | 坏计划可追溯回退模板 | ✅ | `planning/planner.py` | 回退原因写入计划元数据 |
| PL-06 | 计划驱动 ReAct | ✅ | `react/engine.py`, `core/controller.py` | 计划注入首轮；成功工具步推进进度 |
| PL-07 | 计划进度持久化 | ✅ | `core/controller.py` | `runtime_manifest.execution_plan` / `plan_progress`（done/failed/pending） |
| PL-08 | 失败步回退调整 | ✅ | `planning/planner.py` | 追加恢复步骤并记录 `change_reasons`，目标与硬约束不变 |

### 2.3 模型层（PRD 17-20 / Arch 7）

| ID | 功能 | 状态 | 代码位置 | 说明 / 限制 |
|---|---|---|---|---|
| MD-01 | 统一模型适配器契约 | ✅ | `models/base.py` | Mock 与真实模型同契约 |
| MD-02 | Mock 适配器（无 Key 全链路） | ✅ | `models/mock.py` | demo / 测试默认 |
| MD-03 | OpenAI 兼容适配器 | ✅ | `models/openai_compat.py` | 支持 OpenAI/DeepSeek/Moonshot/vLLM 等兼容端点；含网关兼容头 |
| MD-04 | 模型路由 | ✅ | `models/router.py` | 按 intent/domain；显式路由与可用性过滤优先 |
| MD-05 | 质量优先评分 | ✅ | `models/router.py` | 质量 0.70 / 成本 0.15 / 时延 0.15，附 `rank()` 解释 |
| MD-06 | 模型档案 / 迁移步骤表 | ✅ | `models/` | 真实 Key 联调已验证（DeepSeek 兼容端点） |
| MD-07 | 路由结果落盘 | ✅ | `core/controller.py` | `runtime_manifest.model_route` |
| MD-08 | 模型迁移执行链 | ✅ | `models/migration.py`, `app.py` | 九步：档案/资产继承 → 兼容性 → 同数据集回归对比 → 发布组合 → 受控启用 → 回滚；状态 `planned/compatible/evaluated/ready/applied/blocked/rolled_back` |
| MD-09 | 固定模型覆盖路由 | ✅ | `models/router.py`, `core/controller.py` | `model_override` 解析失败回退默认路由并写 `model_override_unavailable` 事件 |
| MD-10 | Claude 全局配置回退 | ✅ | `config.py` | `EVOAGENT_PROVIDER=openai` 且未显式给 Key 时，读取 `~/.claude/settings.json` 的 Key/端点/模型；端点自动补 `/v1`；显式环境变量优先；Key 不落仓库/日志 |

### 2.4 工具系统（PRD 14-16）

| ID | 功能 | 状态 | 代码位置 | 说明 / 限制 |
|---|---|---|---|---|
| TL-01 | 工具契约校验（入参/出参 schema） | ✅ | `tools/contract.py` | 出参 contract 校验 |
| TL-02 | 工具注册表 | ✅ | `tools/registry.py` | 按专家/意图过滤 |
| TL-03 | 五层结果区分 + 错误分类 | ✅ | `tools/contract.py` | 成功/失败/拒绝/不可用/超时 |
| TL-04 | 真实超时（工作线程） | ✅ | `tools/registry.py` | 超时终止等待并返回超时结果 |
| TL-05 | 重试安全规则 | ✅ | `tools/registry.py` | 仅无副作用或带幂等键可重试 |
| TL-06 | 幂等执行日志持久化 | ✅ | `core/persistence.py`, `tools/registry.py` | 命中回放标记 `replayed` |
| TL-07 | `current_time` 当前时间 | ✅ | `tools/builtin.py` | 只读 |
| TL-08 | `calculate` 四则运算 | ✅ | `tools/builtin.py` | 受限表达式求值 |
| TL-09 | `http_get` HTTP 读取 | ✅ | `tools/builtin.py` | 可选 host 白名单 |
| TL-10 | `fs_list` / `file_read` | ✅ | `tools/builtin.py` | 工作目录隔离 |
| TL-11 | `file_write` 写文件 | ✅ | `tools/builtin.py` | 高风险需确认；路径穿越防护；回读校验 + sha256 |
| TL-12 | `git_read` 只读 Git | ✅ | `tools/builtin.py` | 子命令白名单，禁 shell/网络；ref/path 校验 |
| TL-13 | 高风险工具 confirm / deny | ✅ | `core/permissions.py`, `tools/registry.py` | 默认 `confirm`，无确认回调即拒绝 |
| TL-14 | 工具替代链（Arch 6.4） | ✅ | `tools/registry.py`, `tools/contract.py` | 仅可重试错误（timeout/network/tool_defect）且副作用未发生时切备用工具；禁止副作用等级/权限扩大与输入不兼容；主工具或备用工具已执行/副作用不确定时立即停止；过程写入 `substitution_attempts` |

### 2.5 记忆与上下文（PRD 21-33 / Arch 8）

| ID | 功能 | 状态 | 代码位置 | 说明 / 限制 |
|---|---|---|---|---|
| MM-01 | 记忆类型/归属/可信状态/过期 | ✅ | `memory/store.py` | SQLite 持久化 |
| MM-02 | 冲突替代（superseded） | ✅ | `memory/store.py` | 保留替代链 |
| MM-03 | 记忆关系（refines 等） | ✅ | `memory/store.py` | 持久化与查询 |
| MM-04 | 混合相关性检索 | ✅ | `memory/store.py` | 词法 + 局部敏感哈希向量；中文 bigram |
| MM-05 | 使用计数与统计 | ✅ | `memory/store.py` | 按 kind/scope/status |
| MM-06 | 上下文装配优先级 | ✅ | `memory/context.py` | 约束/状态/验收永不裁剪 |
| MM-07 | 动态预算 | ✅ | `memory/context.py` | `context_max_chars` + 模型 `context_limit`；记忆 35% / 结果 30% 份额 |
| MM-08 | 结构化压缩 + 摘要 | ✅ | `memory/context.py` | 约束保留率在裁剪前采集 |
| MM-09 | 上下文质量检查 | ✅ | `memory/context.py` | 约束保留/跨域污染/不可信指令/溢出/状态缺失；异常写事件 |
| MM-10 | 真实嵌入向量检索 | ⬜ | — | 当前为词法 + 哈希向量 |
| MM-11 | 记忆质量评测 | ✅ | `memory/evaluation.py` | 写入准确率/召回/精确率/MRR/过期误用/冲突正确性；内置用例 + 报告持久化 |
| MM-12 | 运行反馈与记忆收益 | ✅ | `memory/evaluation.py`, `app.py` | 有/无记忆任务成功率对比、有害/误导反馈率；缺样本标 `insufficient_evidence` |

### 2.6 专家体系（PRD 9-10）

| ID | 功能 | 状态 | 代码位置 | 说明 / 限制 |
|---|---|---|---|---|
| EX-01 | 专家注册表 + 依赖边 | ✅ | `experts/registry.py` | `impact_analysis` |
| EX-02 | 首批 5 专家 | ✅ | `experts/builtin.py` | general / coding / travel / comic_drama / mobile_operation |
| EX-03 | 能力依赖图 | ✅ | `experts/registry.py` | `file_toolset`、`coding_tasks` 等 |
| EX-04 | 专家级工具白名单 | ✅ | `experts/registry.py`, `core/controller.py` | 工具说明按专家生成 |

### 2.7 工作流（PRD 11-13 / 77-79）

| ID | 功能 | 状态 | 代码位置 | 说明 / 限制 |
|---|---|---|---|---|
| WF-01 | DAG 工作流引擎 | ✅ | `workflows/engine.py`, `workflows/models.py` | 依赖校验、无环 |
| WF-02 | 按依赖分层并行 | ✅ | `workflows/engine.py` | 同层并行 |
| WF-03 | 条件跳过（SKIPPED） | ✅ | `workflows/engine.py` | |
| WF-04 | 节点超时 | ✅ | `workflows/engine.py` | |
| WF-05 | 逐批检查点 + 续跑 | ✅ | `workflows/engine.py` | 复用已完成节点输出 |
| WF-06 | 资产库持久化/加载 | ✅ | `workflows/library.py` | `workflow_defs` 表 |
| WF-07 | AI 漫剧示例 DAG | ✅ | `workflows/builtin.py` | 服务适配器可替换 |
| WF-08 | 暂停任务转 SUSPENDED | ✅ | `workflows/engine.py` | |
| WF-09 | 成功任务抽取候选（Arch 16.2） | ✅ | `workflows/extraction.py` | 按 `(intent, 成功工具序列)` 聚合；排除评测来源与已有 `workflow_ref` 任务；默认 `min_support=2`，单次成功不构成候选 |
| WF-10 | 候选评测与动态输入识别 | ✅ | `workflows/extraction.py` | 结构校验、工具可用性、必填参数绑定（`runnable`）、计划/实际工具调用成本对照 |
| WF-11 | 批准/拒绝/发布门禁 | ✅ | `workflows/extraction.py` | 只能 `candidate → approved → published`；未批准不可发布；必填参数未绑定拒绝发布；状态与决策写审计 |
| WF-12 | 发布后注册可执行 | ✅ | `workflows/extraction.py`, `app.py` | 发布写入 `workflow_defs` 并注册到运行时 `_workflows`，可被 `workflow_ref` 直接调度 |

### 2.8 验收（PRD 12-13 / 36）

| ID | 功能 | 状态 | 代码位置 | 说明 / 限制 |
|---|---|---|---|---|
| AC-01 | 验收器 | ✅ | `acceptance/verify.py` | 空 criteria 默认 `result_nonempty` |
| AC-02 | 验收门禁与固化工件 | ✅ | `acceptance/verify.py`, `core/controller.py` | 未过验收不判成功 |

### 2.9 诊断与故障分类（PRD 41-45 / Arch 9）

| ID | 功能 | 状态 | 代码位置 | 说明 / 限制 |
|---|---|---|---|---|
| DG-01 | 失败自动沉淀诊断案例 | ✅ | `evolution/diagnostics.py`, `core/controller.py` | 规划失败/执行失败/控制器异常均沉淀 |
| DG-02 | A/B/C/D 多假设规则 | ✅ | `evolution/diagnostics.py` | 支持/反对证据 + 不确定性；置信度是评分 |
| DG-03 | 6 个固定探针 | ✅ | `evolution/probes.py` | 有界、无外部网络、临时目录副作用 |
| DG-04 | 探针故障注入 | ✅ | `evolution/probes.py` | `error/timeout/fail` |
| DG-05 | 实验限额 | ✅ | `evolution/probes.py`, `config.py` | 每案 3 次、每日 50 次、单探针 10s |
| DG-06 | 趋势聚合 | ✅ | `evolution/diagnostics.py` | 按状态/意图/类别/组件 |
| DG-07 | 假设显式升级候选 | ✅ | `evolution/controller.py` | 风险 L1-L4 映射；仍需门禁 |
| DG-08 | 诊断不自动发布 | ✅ | `evolution/controller.py` | 硬约束 |
| DG-09 | LLM 辅助假设生成 | ✅ | `evolution/diagnostics.py`, `api/server.py`, `cli.py` | 显式调用 `POST /diagnostics/{id}/hypotheses` 或 `diagnose-assist`；每条必须引用证据快照片段，置信度封顶 0.5、状态 `proposed`，不改案例状态；与规则假设重复则跳过；模型假设须先经固定探针验证才能升级候选（Arch 9.4） |
| DG-10 | 环境漂移检测 | ✅ | `evolution/drift.py`, `core/metrics.py`, `core/alerts.py` | 工具成功率/契约错误/模型行为/任务分布/组件与依赖版本；当前窗 vs 基线窗；处置建议 stop/health_check/compatible_path/evolution |

### 2.10 进化与评测（PRD 46-54 / Arch 10-11）

| ID | 功能 | 状态 | 代码位置 | 说明 / 限制 |
|---|---|---|---|---|
| EV-01 | 候选生命周期状态机 | ✅ | `evolution/candidates.py`, `evolution/controller.py` | 与 Arch 10.4 一致 |
| EV-02 | 候选提案/升级 | ✅ | `evolution/controller.py` | API/CLI 全通 |
| EV-03 | 十维收益评价 | ✅ | `evolution/benefit.py` | 质量（难度加权）/成功率/稳定性/恢复/成本/时延/安全/维护复杂度/跨领域/长期；缺证据标 `insufficient_evidence` |
| EV-04 | 评测集元数据/版本/适用范围 | ✅ | `evolution/datasets.py` | |
| EV-05 | 独立保留集 | ✅ | `evolution/datasets.py`, `evolution/controller.py` | 只用于门禁决策 |
| EV-06 | 基线与候选对照 | ✅ | `evolution/controller.py`, `evolution/evaluator.py` | 基线数据/版本不等价直接拒绝 |
| EV-07 | 确定性安全套件 | ✅ | `evolution/safety.py` | 风险拒绝/确认权威/硬预算/隐私四类检查 |
| EV-08 | 发布门禁 ReleaseGate | ✅ | `evolution/evaluator.py` | 硬难度回归、约束违规、工具失败率、恢复成功率、跨领域回归、安全 fail-closed |
| EV-09 | 评测报告持久化 | ✅ | `core/persistence.py` | `eval_reports` 含元数据 |
| EV-10 | 候选实验预算 | ✅ | `evolution/controller.py`, `evolution/runner.py` | 双记账 `candidate:<id>` + `evolution`；耗尽记失败不中断批次 |
| EV-11 | 候选代码隔离沙箱 | ⬜ | — | PRD 47.4 / L3 |
| EV-12 | 发布后监测采集与判定 | ✅ | `evolution/monitor.py`, `evolution/controller.py` | 时间窗采样、上窗对比、评估任务剔除、安全事件即时回滚、warning hold、`auto_rollback` 可关 |

### 2.11 发布管理（PRD 59-63 / 80-82）

| ID | 功能 | 状态 | 代码位置 | 说明 / 限制 |
|---|---|---|---|---|
| RL-01 | 发布清单 | ✅ | `release/manager.py` | |
| RL-02 | 签名与验证 | ✅ | `release/manager.py` | `EVOAGENT_RELEASE_SECRET` |
| RL-03 | 灰度接口 | ✅ | `release/manager.py` | |
| RL-04 | 回滚 | ✅ | `release/manager.py`, `api/server.py`, `cli.py` | `POST /releases/rollback` |
| RL-05 | L0-L4 风险分级发布规则 | ✅ | `evolution/controller.py` | 核心安全边界改动禁止普通发布 |
| RL-06 | 发布后监测接指标源 | ✅ | `evolution/monitor.py`, `evolution/controller.py` | 运行时任务/事件自动采集；候选 `MONITORING` → `PROMOTED`/`ROLLED_BACK` |
| RL-07 | 自动分支/合并 | ⬜ | — | 当前仅 git 基线（main） |

### 2.12 预算与成本（PRD 55-58 / Arch 12）

| ID | 功能 | 状态 | 代码位置 | 说明 / 限制 |
|---|---|---|---|---|
| BG-01 | 单任务硬预算（模型/工具/成本/时长/并发） | ✅ | `core/budget.py`, `config.py` | 模型无法绕过 |
| BG-02 | 单次模型调用成本上限 | ✅ | `core/budget.py` | 越界在下一次调用前抛出，任务判失败 |
| BG-03 | 单工具次数/成本（可按工具覆盖） | ✅ | `core/budget.py` | `tool_limits` |
| BG-04 | 工作流作用域预算 | ✅ | `core/budget.py` | `ScopedBudget` 父 + 本地；用量双记账 |
| BG-05 | 候选实验预算 | ✅ | `core/budget.py`, `evolution/controller.py` | |
| BG-06 | 进化日/月池 | ✅ | `core/budget.py`, `evolution/controller.py` | 跨实验累计；耗尽直接拒绝候选 |
| BG-07 | 模型提供方池 | ✅ | `core/budget.py`, `core/controller.py` | 跨任务累计 |
| BG-08 | 领域专家池 | ✅ | `core/budget.py`, `core/controller.py` | |
| BG-09 | 成本报告 | ✅ | `core/budget.py` (`CostReporter`) | 按任务/领域/模型/版本/实验；每次成功成本、失败浪费 |
| BG-10 | 进化收益增量 | ✅ | `evolution/controller.py` | `cost_delta`/`latency_delta` 相对基线 |
| BG-11 | 预算报表 API/CLI | ✅ | `api/server.py`, `cli.py` | `GET /costs`、`cli budget`、`cli costs` |

### 2.13 事件、幂等与离线同步（PRD 37-40 / 72-76）

| ID | 功能 | 状态 | 代码位置 | 说明 / 限制 |
|---|---|---|---|---|
| EVT-01 | 事件模型（含隐私/关联字段） | ✅ | `core/events.py` | 与 Arch 9.1 对齐 |
| EVT-02 | 旧库自动补列 | ✅ | `core/events.py` | `PRAGMA table_info` + `ALTER TABLE` |
| EVT-03 | 事件幂等摄取 | ✅ | `core/events.py`, `api/events_sync.py`, `api/server.py` | `event_id` 主键 `INSERT OR IGNORE` |
| EVT-04 | 批次校验 | ✅ | `api/events_sync.py` | 1–200 条、id 格式、±7 天、payload ≤16 KiB、仅 `standard` |
| EVT-05 | 任务幂等提交 | ✅ | `core/persistence.py`, `api/server.py` | `client_request_id` → `client_requests` 表 |
| EVT-06 | 并发幂等 | ✅ | `api/server.py` | 提交锁 + `INSERT OR IGNORE` 后查询 |
| EVT-07 | 隐私分级集中定义 | ✅ | `core/privacy.py` | standard / sensitive / secret |

### 2.14 控制面、API 与可观测（PRD 37-40 / 89-90）

| ID | 功能 | 状态 | 代码位置 | 说明 / 限制 |
|---|---|---|---|---|
| CP-01 | REST API（stdlib ThreadingHTTPServer） | ✅ | `api/server.py` | 见 §3 |
| CP-02 | Bearer 鉴权（普通/管理双令牌） | ✅ | `api/auth.py` | `EVOAGENT_API_TOKEN` / `EVOAGENT_ADMIN_TOKEN` |
| CP-03 | 审计日志 | ✅ | `core/audit.py` | 进化/发布变更写审计 |
| CP-04 | 指标 | ✅ | `core/metrics.py` | JSON + Prometheus（`?format=prometheus`）；无样本指标输出 `NaN` 保持序列稳定 |
| CP-05 | 管理页数据接口 | ✅ | `api/admin.py` | `GET /admin`（HTML）：只读观测 + 任务回放 + 备份/恢复面板 |
| CP-06 | 健康检查 | ✅ | `api/server.py`, `app.py` | 含专家、预算、诊断计数 |
| CP-07 | Web 管理端完整界面 | ⬜ | — | 观测/回放/备份操作已可用；编辑与可视化浏览待迭代 |
| CP-08 | 四层日志分层 | ✅ | `core/log_layers.py` | runtime_event / execution_trace / diagnostic_evidence / audit，独立保留期与读取角色 |
| CP-09 | 执行轨迹 | ✅ | `core/log_layers.py` (`ExecutionTracer`) | 订阅事件总线，按任务连续 `step` 落盘；敏感事件只留脱敏标记 |
| CP-10 | 诊断证据授权 | ✅ | `core/log_layers.py` (`EvidenceStore`) | 仅 `standard` + `authorized=True` 可导出；`secret` 拒绝入库 |
| CP-11 | 保留期清理与按任务删除 | ✅ | `core/log_layers.py` (`LayerManager`) | 30/14/7/365 天；删日志保留任务行与审计 |
| CP-12 | 分层访问控制 | ✅ | `api/server.py` | `/logs/evidence` 与清理操作仅 admin；轨迹需 execution_trace 读权限 |
| CP-13 | 扩展指标 | ✅ | `core/metrics.py` | 模型调用成功率、工具契约错误率、ReAct 异常率、恢复、上下文、记忆、进化/回滚、意图维度 |
| CP-14 | 分级告警 | ✅ | `core/alerts.py` | 8 条规则（成功率/连续失败/模型/契约/成本/回滚/ReAct/安全），`GET /alerts` |
| CP-15 | 模型调用计量 | ✅ | `models/metering.py` (`MeteredModel`) | 包装适配器，记录 `model_id/op/ok/error_class/latency_s` 不影响原行为 |
| CP-16 | 任务运行回放（Arch 20.3） | ✅ | `core/replay.py`, `api/server.py`, `cli.py` | 从事件/轨迹/检查点/运行记录重建时间线；`read_only=True`、`reexecute_side_effects=False`；非 `standard` 负载脱敏 |
| CP-17 | 备份与恢复（Arch 20.4） | ✅ | `core/backup.py`, `api/server.py`, `cli.py` | 三库在线备份打包 + `manifest.json`（sha256/行数/版本）；校验与完整性检查；恢复前自动安全备份；存在未结束任务拒绝恢复 |
| CP-18 | 管理端回放/备份面板 | ✅ | `api/admin.py` | 只读回放展示版本/统计/时间线；备份创建、列表与二次确认恢复；动态内容 `textContent` 渲染 |

---

## 3. REST API 清单

鉴权：设置 `EVOAGENT_API_TOKEN` 后普通接口需 `Authorization: Bearer <token>`；管理接口用 `EVOAGENT_ADMIN_TOKEN`。

### GET

| 路径 | 说明 |
|---|---|
| `/` | 服务信息 |
| `/health` | 健康检查（专家、预算、诊断计数） |
| `/version` | 版本 |
| `/tasks` | 任务列表 |
| `/tasks/{id}` | 任务详情 |
| `/tasks/{id}/events` | 任务事件 |
| `/tasks/{id}/replay?limit=N` | 只读运行回放（需 `execution_trace` 读权限；不重放副作用；敏感负载脱敏） |
| `/experts` | 专家与能力图 |
| `/metrics` | 指标（JSON；`?format=prometheus`） |
| `/audit` | 审计日志 |
| `/costs?limit=N` | 成本报告（totals/by_task/by_domain/by_model/evolution） |
| `/workflows`、`/workflows/{id}` | 工作流资产库 |
| `/workflows/candidates?status=&limit=` | 抽取候选列表（可按状态过滤） |
| `/workflows/candidates/{id}` | 候选详情（签名/支持数/定义/评测/决策） |
| `/memory/stats` | 记忆统计 |
| `/memory/evaluations?limit=N` | 记忆评测报告列表 |
| `/models` | 模型档案、默认路由与显式路由 |
| `/models/migrations`、`/models/migrations/{id}` | 模型迁移列表/详情 |
| `/diagnostics`、`/diagnostics/{case_id}`、`/diagnostics/trends` | 诊断案例/详情/趋势 |
| `/diagnostics/drift` | 环境漂移检测报告（只读，不产生事件） |
| `/evolution/candidates`、`/evolution/candidates/{id}`、`/evolution/datasets` | 进化候选与数据集 |
| `/releases`、`/releases/{id}` | 发布清单 |
| `/alerts` | 当前告警（按 `AlertPolicy` 评估） |
| `/logs/layers` | 四层日志策略、计数与当前角色可读性 |
| `/logs/traces?task_id=&limit=` | 执行轨迹（需 execution_trace 权限） |
| `/logs/evidence?task_id=&limit=` | 诊断证据（admin；返回 `exportable` 标记） |
| `/admin` | 只读管理页（含回放与备份/恢复操作，admin 接口需管理令牌） |
| `/admin/backups` | 备份列表（admin） |

### POST

| 路径 | 说明 |
|---|---|
| `/tasks` | 提交任务；可选 `client_request_id` 幂等（重复返回原 `task_id` + `202 deduplicated`） |
| `/tasks/{id}/cancel` | 取消 |
| `/tasks/{id}/pause` / `/tasks/{id}/resume` | 暂停/恢复（`409 requires_force` 表示需显式 force） |
| `/events/sync` | 批量事件摄取 → `accepted` / `duplicates` / `rejected` |
| `/workflows/extract` | 从成功任务抽取候选（可选 `min_support` / `limit`；参数非法 `400`） |
| `/workflows/candidates/{id}/approve` | 批准候选（admin；评测未通过 `409`） |
| `/workflows/candidates/{id}/reject` | 拒绝候选（admin；非法状态 `409`） |
| `/workflows/candidates/{id}/publish` | 发布候选（admin；未批准/未绑定必填参数 `409`） |
| `/logs/purge` | 按保留期清理各层（admin，写审计） |
| `/logs/delete-task` | 删除单任务日志与证据（admin，保留任务行，写审计） |
| `/evolution/candidates` | 候选提案 |
| `/evolution/candidates/{id}/evaluate` | 评测 |
| `/evolution/candidates/{id}/approve` | 批准 |
| `/evolution/candidates/{id}/deploy` | 发布 |
| `/evolution/candidates/{id}/monitor` | 监测（无指标时自动采集；无数据源返回 `409`） |
| `/evolution/candidates/{id}/rollback` | 回滚 |
| `/releases/rollback` | 发布回滚 |
| `/diagnostics/scan` | 扫描失败任务生成案例 |
| `/diagnostics/drift/scan` | 检测并显式发出 `drift_detected` 事件 |
| `/memory/evaluate` | 运行记忆质量评测并持久化报告 |
| `/models/migrations` | 创建迁移（未知模型 `404`，同模型 `400`，不兼容 `409`） |
| `/models/migrations/{id}/regression` | 同数据集回归对比 |
| `/models/migrations/{id}/release` | 生成发布组合 |
| `/models/migrations/{id}/apply` | 受控启用（`gray_percent`、`actor`） |
| `/models/migrations/{id}/rollback` | 回滚迁移（`reason`） |
| `/diagnostics/{case_id}/experiments` | 运行探针实验（可选 `fault: error|timeout|fail`） |
| `/diagnostics/{case_id}/hypotheses` | 模型辅助假设（advisory；无模型 `503`；证据未绑定/重复项进 `skipped`） |
| `/diagnostics/{case_id}/candidates` | 假设升级候选 |
| `/admin/backup` | 创建三库备份（admin，写审计，`201`） |
| `/admin/restore` | 恢复指定备份（admin；未结束任务 `409`、缺失 `404`、非法名称 `400`；恢复前自动安全备份） |

---

## 4. CLI 清单

入口：`python -m evoagent.cli <command>`。

| 命令 | 说明 |
|---|---|
| `demo` | 无 Key 全链路演示（意图→规划→ReAct→工具→验收→记忆） |
| `task "<goal>" [--intent ...]` | 直接提交任务 |
| `workflow [name]` | 运行工作流（默认漫剧 DAG） |
| `status <task_id>` | 查询任务 |
| `pause <task_id>` / `resume <task_id> [--force]` | 暂停/恢复 |
| `diagnose [task_id] [--probe ...]` | 诊断案例/探针 |
| `diagnose-assist <case_id> [--limit N]` | 模型辅助假设（需真实模型；仅 advisory） |
| `serve [--port N]` | 启动 REST API（默认 8300） |
| `candidates` / `propose` / `evaluate` / `approve` / `deploy` / `monitor` | 进化候选操作 |
| `workflows` | 工作流资产库 |
| `workflow-extract [--min-support N] [--limit N]` | 从成功任务抽取候选 |
| `workflow-candidates [--status] [--limit]` | 候选列表 |
| `workflow-evaluate <id> [--args JSON]` | 查看结构/工具/绑定/成本评测 |
| `workflow-approve <id> [--reason]` / `workflow-reject <id> [--reason]` | 批准/拒绝 |
| `workflow-publish <id> [--args JSON] [--reason]` | 发布（必填参数经 `args_by_step` 绑定） |
| `budget` | 全部预算层级限额 + 运行用量 |
| `costs` | 成本报告 |
| `releases` / `release-rollback` | 发布清单/回滚 |
| `logs [--layer] [--task] [--limit] [--purge] [--delete-task]` | 分层日志查看/清理/按任务删除 |
| `alerts` | 当前告警（含漂移状态） |
| `migrate <old> <new> [--dataset] [--apply] [--gray N]` | 模型迁移：兼容性 → 回归 → 发布组合 →（可选）启用 |
| `migrations [migration_id]` | 迁移列表/详情 |
| `memory-eval [--no-persist]` | 记忆质量评测与运行反馈 |
| `drift [--window N] [--emit]` | 环境漂移检测 |
| `replay <task_id> [--limit N]` | 只读运行回放（时间线/统计/脱敏标记） |
| `backup [create|verify|restore|list] [ref] [--name]` | 备份创建/校验/恢复/列表（恢复前检查未结束任务） |

---

## 5. Android 客户端

工程：React Native 0.75.4，TypeScript。已验证：Gradle 构建通过，模拟器 + 真机（HyperOS / Android API 36）安装运行。

### 5.1 界面（`android-app/App.tsx`）

| ID | 功能 | 状态 | 说明 |
|---|---|---|---|
| UI-01 | 服务端地址可编辑并持久化 | ✅ | AsyncStorage，`evoagent_base_url` |
| UI-02 | 在线/离线状态条 + 手动同步 | ✅ | |
| UI-03 | 同步统计（待发/失败/死信） | ✅ | |
| UI-04 | 任务提交（在线/离线） | ✅ | 离线保存为 `LOCAL_ONLY` 并注明可执行范围 |
| UI-05 | 本地任务列表 | ✅ | 状态、重试、对账 |
| UI-06 | `NEEDS_RECONCILE` 用户确认 | ✅ | 崩溃恢复后不盲重试 |
| UI-07 | 取消任务 | ✅ | |
| UI-08 | 死信手动重试 | ✅ | |
| UI-09 | 无障碍服务开关检测与授权引导 | ✅ | |
| UI-10 | 低风险显式 Intent 动作 | ✅ | |
| UI-11 | 中风险按文本点击 | ✅ | 精确匹配优先、子串回退 |
| UI-12 | 设备操作 journal 展示 | ✅ | prepared/dispatched/verified/… |
| UI-13 | 只读系统状态面板 | ✅ | 任务指标、预算、发布、进化候选 |
| UI-14 | 5 秒轮询（仅在线） | ✅ | 离线不发网络请求 |
| UI-15 | 视觉/OCR 操作 | ⬜ | |
| UI-16 | 语音入口 | ⬜ | |
| UI-17 | 后台常驻同步 | ⬜ | 当前仅前台 + 手动 |

### 5.2 离线核心（`android-app/src/offline/`，纯 TS 可单测）

| ID | 功能 | 状态 | 代码位置 | 说明 |
|---|---|---|---|---|
| OF-01 | 注入式存储 + 整快照原子写 | ✅ | `storage.ts` | `offline_state_v1`；RN 适配见 `src/storage/asyncStorage.ts` |
| OF-02 | 本地事件队列 | ✅ | `queue.ts` | FIFO、去重、容量 500、`seq` 单调 |
| OF-03 | 连续 checkpoint | ✅ | `queue.ts` | 只推进连续已确认前缀，不跳空洞 |
| OF-04 | 死信队列 | ✅ | `queue.ts` | 不静默丢弃，可手动重试 |
| OF-05 | 隐私过滤 | ✅ | `privacy.ts` | 仅 `standard` 上传；`sensitive` 本地保留；`secret/private` 入队前拒绝 |
| OF-06 | 指数退避同步 | ✅ | `sync.ts` | 2s→5min，带抖动；`flush()` 手动触发 |
| OF-07 | 任务 + 事件单次落盘 | ✅ | `tasks.ts` | 要么都落盘要么都不落 |
| OF-08 | 离线任务 `LOCAL_ONLY` → `SUBMITTING` | ✅ | `tasks.ts` | 上线后带稳定 `client_request_id` 提交 |
| OF-09 | `SUBMITTING` 崩溃恢复 → `NEEDS_RECONCILE` | ✅ | `tasks.ts` | 需用户确认后重提 |
| OF-10 | 离线可执行策略 | ✅ | `policy.ts` | 当前仅 `time_query` 用设备本地时间完成，不上传 |
| OF-11 | 时间戳单位转换 | ✅ | `apiTransport.ts` | 客户端毫秒 → 服务端秒 |
| OF-12 | 端到端加密 | ⬜ | — | |
| OF-13 | 多设备冲突合并 | ⬜ | — | |

### 5.3 设备操作（`android-app/src/device/` + Kotlin）

| ID | 功能 | 状态 | 代码位置 | 说明 |
|---|---|---|---|---|
| DV-01 | 风险分级 | ✅ | `broker.ts` | 发送/支付/删除/通话/卸载 = high；点击 = medium |
| DV-02 | 高风险独立确认 | ✅ | `broker.ts` | 不依赖模型提示词 |
| DV-03 | 动作 journal | ✅ | `broker.ts` | prepared → dispatched → verified/failed/denied/unverified |
| DV-04 | 结果五态 | ✅ | `types.ts`, `broker.ts` | `verified` 必须有证据 |
| DV-05 | 已下发未验证禁止自动重放 | ✅ | `broker.ts` | |
| DV-06 | 无 NativeModule → `unavailable` | ✅ | `nativeTransport.ts` | 不崩溃 |
| DV-07 | AccessibilityService 节点点击 | ✅ | `DeviceAccessibilityService.kt` | 按文本/描述，精确优先；无手势注入 |
| DV-08 | DeviceModule 原生桥 | ✅ | `DeviceModule.kt`, `DevicePackage.kt` | `isServiceEnabled` / `launchIntent` / `performClick` |
| DV-09 | 原生壳修复 | ✅ | `MainActivity.kt`, `MainApplication.kt`, `AndroidManifest.xml` | 合法权限、Service 注册、RN 0.75.4 兼容 |
| DV-10 | 无障碍配置 | ✅ | `res/xml/accessibility_service_config.xml` | `canRetrieveWindowContent=true`，不做手势 |
| DV-11 | 坐标注入 / 截屏模拟 | ⬜ | — | 明确不做，不绕过系统权限 |

### 5.4 客户端脚本

| 脚本 | 说明 |
|---|---|
| `npm run android` | 构建并安装 |
| `npm start` | Metro |
| `npm run test:offline` | Node 24 原生 TS 测试（`node --test test/offline test/device`） |
| `npm run typecheck` | `tsc --noEmit` |
| `npm run ota:publish` | OTA 发布（Expo Updates / CodePush 路线占位） |

---

## 6. 数据与持久化

`core/persistence.py`，SQLite（标准库 `sqlite3`），默认数据目录 `.evoagent/`（`EVOAGENT_DATA` 可改）。

| 表 | 内容 |
|---|---|
| `tasks` | 任务实体、状态、manifest、结果证据 |
| `task_runs` | 每次执行/重跑记录 |
| `task_checkpoints` | 检查点 |
| `workflow_runs` / `workflow_defs` | 工作流运行与资产库 |
| `workflow_candidates` | 成功任务抽取候选（签名/支持数/定义/评测/决策） |
| `client_requests` | 任务幂等键 → task_id |
| `runtime_events` | 运行事件（含隐私/关联/同步字段） |
| `execution_traces` | 执行轨迹（任务 `step` 连续、隐私分级、按 `at` 索引） |
| `diagnostic_evidence` | 诊断证据（引用、授权标记、保留 7 天） |
| `memory_items` | 记忆、关系、使用计数 |
| `tool_executions` | 幂等执行日志 |
| `budget_ledger` | 预算账本（scope + 日/月键） |
| `diagnostic_cases` / `diagnostic_hypotheses` / `diagnostic_experiments` | 诊断 |
| `evolution_candidates` | 候选生命周期 |
| `model_migrations` | 模型迁移执行链（步骤、兼容性、回归对比、状态） |
| `memory_eval_reports` | 记忆评测报告（指标、用例明细、运行反馈） |
| `eval_reports` | 评测报告（含元数据） |
| `release_manifests` / `release_events` | 发布清单与事件 |
| `audit_log` | 审计 |

兼容策略：旧库启动时按 `PRAGMA table_info` 自动补列（如 `runtime_events`）；结构不兼容时重建对应表。

备份目录：`<data_dir>/backups/`，每个 zip 含三库与 `manifest.json`（格式版本、文件 sha256/字节数、表行数、发布 ID）。恢复前自动生成 `pre_restore_<时间戳>_<随机>.zip`，恢复后建议重启进程（内存缓存不自动刷新）。

---

## 7. 配置项

`evoagent/config.py`。环境变量：

| 变量 | 说明 |
|---|---|
| `EVOAGENT_DATA` | 数据目录 |
| `EVOAGENT_PROVIDER` | `mock`（默认）/ `openai` |
| `EVOAGENT_API_KEY` | 模型 Key（不得写入仓库或文档）；设置后不再复用 Claude 端点 |
| `EVOAGENT_BASE_URL` | OpenAI 兼容端点（如 DeepSeek）；`/v1` 需显式包含 |
| `EVOAGENT_MODEL` | 模型名 |
| `EVOAGENT_CLAUDE_SETTINGS` | Claude 全局配置文件路径，默认 `~/.claude/settings.json` |
| `EVOAGENT_HIGH_RISK` | 高风险策略：confirm（默认）/ deny |
| `EVOAGENT_API_TOKEN` | 普通接口 Bearer |
| `EVOAGENT_ADMIN_TOKEN` | 管理接口 Bearer |
| `EVOAGENT_RELEASE_SECRET` | 发布签名密钥 |

`EVOAGENT_PROVIDER=openai` 且未设置 `EVOAGENT_API_KEY` 时，从 Claude 全局配置读取
`ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL`，端点自动补 `/v1`；
`EVOAGENT_*` 环境变量始终优先。读取失败时保留默认值，不影响 Mock 演示。

预算默认值（`BudgetConfig`）：

| 层级 | 默认限额 |
|---|---|
| 单任务 | 25 次模型 / 50 次工具 / 1.0 成本 / 300s；单次调用 0.25；单工具 10 次 |
| 工作流 | 15 / 40 / 0.60 / 180s |
| 候选实验 | 40 / 100 / 2.0 / 600s |
| 进化日 / 月 | 200 次、5.0 / 2000 次、50.0 |
| 运行时日池 | 2000 次、20.0 |
| 提供方池 | 1000 次、10.0 |
| 专家池 | 500 次、5.0 |
| 并发 | 2 |

上下文：`context_max_chars` 与模型 `context_limit` 共同约束。

诊断：`max_experiments_per_case=3`、`max_daily_experiments=50`、`max_probe_seconds=10`、
`max_assist_hypotheses=3`（模型辅助假设单次上限，运行时再限制为 1-5）。

---

## 8. 测试矩阵

### Python（`tests/`，266 项）

| 文件 | 覆盖范围 |
|---|---|
| `test_state_machine.py` | 状态机合法/非法迁移 |
| `test_budget.py` | 基础预算 |
| `test_budget_hierarchy.py` | 九级预算、池、候选双记账、成本报告（13 项） |
| `test_runtime.py` | 端到端运行时 |
| `test_control.py` | 计量、限额、计划持久化、路由、暂停恢复、重跑、副作用闸门（9 项） |
| `test_planning.py` | 计划校验/回退、意图分类（具体领域提示、`time_query` 注册） |
| `test_config.py` | Claude 全局配置回退、`/v1` 归一、显式环境变量优先、缺失/损坏配置降级（7 项） |
| `test_tools.py` | 契约、权限、副作用、幂等 |
| `test_tool_workflow.py` | 工具超时/重试/输出契约、工作流并行/跳过/续跑 |
| `test_tool_substitution.py` | 工具替代安全链：可重试失败切换、副作用/权限/输入兼容校验、不确定副作用阻断、已执行失败停止链（8 项） |
| `test_workflow_extraction.py` | 支持度门槛、去重、评测排除、批准/发布门禁、参数绑定与注册（11 项） |
| `test_workflow_api.py` | 候选 REST 生命周期、admin 动作、404/409（2 项） |
| `test_memory.py` | 记忆存储、替代、检索 |
| `test_memory_router_diag.py` | 记忆关系、路由评分、诊断趋势/故障注入 |
| `test_workflow.py` | DAG 引擎 |
| `test_diagnostics.py` | 案例/假设/探针/升级 |
| `test_diagnostic_assist.py` | 模型辅助假设：证据绑定、置信度封顶、去重、无效 JSON/模型异常降级、探针验证后才可升级、REST 200/404/503（11 项） |
| `test_evolution.py` | 候选生命周期、评测、门禁 |
| `test_evolution_benefit.py` | 十维收益、跨领域回归、发布后监测采集/判定/回滚（21 项） |
| `test_governance.py` | 上下文治理、评测完整性、fail-closed 门禁（12 项） |
| `test_event_sync.py` | 事件同步、旧库迁移、幂等提交（8 项） |
| `test_model_migration.py` | 九步迁移、回归对比、受控启用/回滚、固定模型覆盖、迁移 API（10 项） |
| `test_memory_evaluation.py` | 记忆评测指标、过期/冲突用例、运行反馈、报告 API/指标（6 项） |
| `test_drift.py` | 漂移信号与处置建议、版本变更、事件发射、指标/告警/API（9 项） |
| `test_observability.py` | 分层策略/脱敏、执行轨迹、证据导出授权、保留清理、扩展指标、告警、REST 访问控制（17 项） |
| `test_replay.py` | 回放重建、敏感负载脱敏、不重放副作用、未知任务、API 200/404（6 项） |
| `test_backup.py` | 打包/校验/篡改检测、恢复往返、安全备份、路径校验、API 409/404（7 项） |
| `test_control_plane.py` | 鉴权、审计、指标、管理页（含回放/备份面板） |
| `test_release.py` | 清单、签名、回滚 |
| `test_api.py` | API 冒烟 + `/costs` |

### 客户端（`android-app/test/`，29 项）

| 文件 | 覆盖范围 |
|---|---|
| `test/offline/queue.test.ts` | FIFO、去重、容量、checkpoint 连续性、死信、ack |
| `test/offline/sync.test.ts` | 退避、不丢事件、隐私过滤、部分成功 |
| `test/offline/tasks.test.ts` | 离线提交、幂等键稳定、`NEEDS_RECONCILE`、离线 `time_query` |
| `test/offline/transport.test.ts` | 时间戳单位转换 |
| `test/device/broker.test.ts` | 风险闸门、确认、五态、不重放、unavailable |

运行：

```bash
python -m pytest tests/ -q
cd android-app && npm run test:offline
```

---

## 9. 未实现 / 占位清单

| 项 | PRD / Arch | 状态 | 说明 |
|---|---|---|---|
| 真实嵌入向量检索 | PRD 25 | ⬜ | 当前词法 + 哈希向量 |
| LLM 辅助诊断假设 | PRD 41-45 / Arch 9.4 | ✅ | 模型输出仅作证据绑定的辅助解释；不改案例状态，探针验证前不可升级候选 |
| 候选代码隔离沙箱 | PRD 47.4 / L3 | ⬜ | 评测执行器运行受控任务，不是任意代码沙箱 |
| 视觉/OCR/语音操作 | PRD 64/68 | ⬜ | 仅无障碍节点与显式 Intent |
| 后台常驻同步 | PRD 72-76 | ⬜ | 前台 + 手动重试 |
| 端到端加密、多设备冲突合并 | PRD 72-76/86 | ⬜ | |
| Web 管理端完整界面 | PRD 90 | ⬜ | 观测/回放/备份操作已可用，编辑与可视化待迭代 |
| 发布自动分支/合并 | PRD 59-63 | ⬜ | 仅 git 基线 |
| 任务模型全字段 | PRD 7.3 | 🔹 | 核心子集已实现 |

---

## 10. 验证步骤

```bash
# 1. 服务端测试
python -m pytest tests/ -q                       # 255 passed

# 2. 客户端测试 + 类型检查
cd android-app && npm run test:offline && npm run typecheck && cd ..

# 3. 无 Key 全链路
python -m evoagent.cli demo
python -m evoagent.cli workflow
python -m evoagent.cli budget
python -m evoagent.cli costs
python -m evoagent.cli logs
python -m evoagent.cli alerts
python -m evoagent.cli migrate mock-1 mock-2      # 兼容性 → 回归 → 发布组合
python -m evoagent.cli migrations
python -m evoagent.cli memory-eval --no-persist
python -m evoagent.cli drift
python -m evoagent.cli replay <task_id>          # 只读回放
python -m evoagent.cli backup create             # 备份 → verify → list
# 同一意图重复成功 ≥2 次后：
python -m evoagent.cli workflow-extract
python -m evoagent.cli workflow-evaluate <candidate_id>
python -m evoagent.cli workflow-approve <candidate_id> --reason "抽检通过"
python -m evoagent.cli workflow-publish <candidate_id> --args '{"step_1":{"expression":"3+4"}}'

# 4. API 冒烟
python -m evoagent.cli serve --port 8300
#   - 两次相同 client_request_id 的 POST /tasks → 同一 task_id
#   - 重复 POST /events/sync → duplicates
#   - GET /costs /metrics /admin /alerts /logs/layers /logs/traces
#   - GET /models /models/migrations /memory/evaluations /diagnostics/drift
#   - POST /models/migrations → regression → release → apply → rollback
#   - POST /memory/evaluate、/diagnostics/drift/scan
#   - GET /tasks/{id}/replay、GET /admin/backups
#   - POST /admin/backup → /admin/restore（未结束任务返回 409）
#   - POST /workflows/extract → GET /workflows/candidates
#   - POST /workflows/candidates/{id}/approve → /publish（缺参数返回 409）

# 5. Android（真机/模拟器）
adb reverse tcp:8300 tcp:8300
cd android-app && npm run android
```

客户端已在模拟器与真机（HyperOS / Android API 36）验证：离线队列、事件同步、真实模型任务、无障碍节点点击（`[verified]` + 证据）。

---

## 维护流程

后续开发完成后，按以下步骤更新本文件：

1. **更新功能表**：在 §2/§5 对应领域表中修改状态（✅/🔹/⬜），必要时新增行；ID 一经分配不复用，删除的功能保留行并标 ⬜ 或注明「已移除」。
2. **同步接口/配置/数据**：若新增 REST 路由、CLI 命令、环境变量、数据表或测试文件，更新 §3/§4/§6/§7/§8。
3. **更新测试基线**：修改测试总数与分布。
4. **追加变更记录**：在 §变更记录 末尾按模板追加。
5. **交叉更新**：`README.md`（能力摘要）、`IMPLEMENTATION.md`（PRD 映射）、`progress.md`（会话日志）、`task_plan.md`（阶段）保持同步。
6. **验证后再标 ✅**：仅当有自动化测试或端到端实测时标记 ✅；仅接口就位标 🔹。

变更记录模板：

```markdown
### YYYY-MM-DD — <一句话主题>
- 需求：PRD <条目> / Arch <章节>
- 变更：<新增/修改/修复>
- 代码：`path/to/file.py`（函数/类）
- 接口/配置：<新增 API/CLI/env/表>
- 测试：<文件与数量>，Python 总数 → N，客户端 → M
- 状态变化：<功能 ID> 🔹 → ✅
- 已知限制：<...>
```

---

## 变更记录

### 2026-10-20 — 模型辅助诊断假设（证据绑定 + 探针门禁）

- 需求：PRD 41-45 / Arch 9.4；迭代待办 #4「LLM 辅助诊断假设」。
- 变更：新增 `DiagnosisEngine.suggest_hypotheses()`。模型只提出候选解释：
  `category/component/statement/evidence_refs/confidence`；每条必须有至少一条
  `evidence_refs` 逐字出现在证据快照（JSON 或规范化行）中，否则丢弃；置信度封顶 0.5、
  状态固定 `proposed`；与已有规则假设 `(category, component)` 重复时跳过；无效 JSON、
  模型异常均降级为空结果，不影响诊断。模型输出不改变案例状态、不触发探针。
- 代码：`evolution/diagnostics.py`（`source` 字段、辅助提示/解析/校验）、
  `config.py`（`max_assist_hypotheses`）、`app.py`（注入真实模型）、
  `api/server.py`（`POST /diagnostics/{case_id}/hypotheses`）、`cli.py`（`diagnose-assist`）。
- 安全：模型假设即使有证据引用也不能直接升级候选，必须先有对应探针实验记录
  （`promote_to_candidate` 拒绝无实验的 `source=model` 假设）。
- 验证：真实网关 `deepseek-v4-flash` 在既有案例上生成 2 条辅助假设、1 条重复被跳过；
  `python -m pytest tests/ -q` → 266 passed。
- 测试：新增 `tests/test_diagnostic_assist.py`（11 项），Python 255 → 266。

### 2026-10-20 — 真实模型复用 Claude 全局配置

- 需求：PRD 17-20 / 4.5；真实模型联调（迭代待办 #1）。
- 变更：`EVOAGENT_PROVIDER=openai` 且未显式提供 Key 时，`Config.load()` 从 Claude 全局
  `settings.json` 读取 `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL`；
  Anthropic 端点自动补 `/v1`；显式 `EVOAGENT_*` 环境变量始终优先；显式 Key 不复用 Claude 端点。
  意图分类提示补充各具体领域描述，`time_query` 注册为已知意图；模型给出的非专家 domain
  归一化为路由后的专家 ID。
- 代码：`config.py`（`_apply_claude_global` / `_openai_base_url`）、`planning/intent.py`、
  `experts/builtin.py`、`core/controller.py`。
- 接口/配置：`EVOAGENT_CLAUDE_SETTINGS`（可选路径）；不改 CLI/API 形态。
- 验证：真实端点 `deepseek-v4-flash` 完成计算与时间任务；意图分类 `time_query` 0.99、
  计划驱动 `current_time` 工具、4 次模型调用/1 次工具调用；Key 未输出或落盘。
- 测试：新增 `tests/test_config.py`（7 项）与 `test_planning.py` 2 项；Python 246 → 255，客户端保持 29。
- 状态变化：新增 MD-10 ✅；迭代待办「真实 LLM 联调」完成。
- 已知限制：仅复用 Claude 配置中的 OpenAI 兼容端点；不解析 `apiKeyHelper` 或其他凭据来源。

### 2026-10-20 — 工具替代安全链 + 成功任务工作流抽取

- 需求：Arch 6.4（工具替代）、Arch 16.2 / PRD 13.4（从成功任务到稳定工作流）。
- 变更：工具契约新增 `substitutes`；注册表在主工具可重试失败且副作用未发生时按序尝试备用工具，拒绝副作用升级/权限扩大/输入不兼容，副作用不确定立即停止。新增工作流抽取器：按意图与成功工具序列聚合、最小支持度、结构/工具/参数绑定评测、批准/拒绝/发布状态机与审计，发布后写入资产库并注册运行时。
- 代码：`tools/contract.py`、`tools/registry.py`（`_substitute_safe`）、`workflows/extraction.py`（`WorkflowExtractor`）、`workflows/models.py`、`core/persistence.py`（`workflow_candidates` 表）、`api/server.py`、`cli.py`、`app.py`。
- 接口/配置：`GET /workflows/candidates`、`/workflows/candidates/{id}`；`POST /workflows/extract`、`/workflows/candidates/{id}/{approve|reject|publish}`（admin）；CLI `workflow-extract`、`workflow-candidates`、`workflow-evaluate`、`workflow-approve`、`workflow-reject`、`workflow-publish`；新表 `workflow_candidates`。
- 测试：新增 `tests/test_tool_substitution.py`（8 项）、`tests/test_workflow_extraction.py`（11 项）、`tests/test_workflow_api.py`（2 项）；Python 总数 225 → 246，客户端保持 29。
- 状态变化：新增 TL-14、WF-09~WF-12 ✅。
- 已知限制：抽取仅覆盖工具节点线性序列（不含条件/自定义函数）；参数绑定需人工提供，未做参数值建议；候选只从成功任务与事件推导，不重放工具。

### 2026-10-20 — 任务运行回放 + 备份与恢复 + 管理端操作面板

- 需求：Arch 20.3 / 20.4；PRD 90。
- 变更：新增只读运行回放（任务/版本/计划/检查点/运行记录/事件与轨迹时间线，敏感负载脱敏，不执行副作用）；新增三库在线备份、校验（sha256 + `PRAGMA integrity_check` + 必需表）与恢复（自动安全备份、未结束任务拒绝恢复）；管理端增加回放与备份/恢复面板。
- 代码：`core/replay.py`（`ReplayService`）、`core/backup.py`（`BackupManager`）、`core/persistence.py`（`list_runs`）、`api/server.py`、`api/admin.py`、`cli.py`、`app.py`。
- 接口/配置：`GET /tasks/{id}/replay`、`GET /admin/backups`；`POST /admin/backup`、`/admin/restore`；CLI `replay`、`backup`；数据目录新增 `backups/`。
- 测试：新增 `tests/test_replay.py`（6 项）、`tests/test_backup.py`（7 项）；Python 总数 212 → 225，客户端保持 29。
- 状态变化：新增 CP-16、CP-17、CP-18 ✅。
- 已知限制：恢复后建议重启进程以刷新内存缓存；回放为事件级重建，非逐 token/屏幕录制回放；备份不含工具产物等外部文件。

### 2026-10-20 — 模型迁移执行链 + 记忆评测 + 环境漂移检测

- 需求：PRD 20 / 28 / 44 / 104 / 111；Arch 7.4 / 8.2 / 9.3 / 9.5。
- 变更：新增模型迁移九步执行链（档案/资产继承、兼容性、同数据集回归对比、发布组合、灰度启用、回滚）与固定模型覆盖路由；新增记忆质量评测（写入准确率/召回/精确率/MRR/过期误用/冲突正确性）与运行反馈（记忆收益、有害/误导率）；新增环境漂移检测（工具成功率、契约错误、模型行为、任务分布、组件与依赖版本）并接入指标、告警、API 与 CLI。
- 代码：`models/migration.py`（`ModelMigrationManager`）、`memory/evaluation.py`（`MemoryEvaluator`、`builtin_memory_cases`）、`evolution/drift.py`（`DriftDetector`、`DriftPolicy`）、`models/router.py`（严格 `resolve()`）、`core/controller.py`（`model_route` / 覆盖回退）、`core/metrics.py`（漂移缓存与 Prometheus 序列）、`core/alerts.py`（`environment_drift`）、`core/persistence.py`（两张新表）、`api/server.py`、`cli.py`、`app.py`。
- 接口/配置：`GET /models`、`/models/migrations`、`/models/migrations/{id}`、`/memory/evaluations`、`/diagnostics/drift`；`POST /models/migrations`、`/models/migrations/{id}/{action}`、`/memory/evaluate`、`/diagnostics/drift/scan`；CLI `migrate`、`migrations`、`memory-eval`、`drift`；新表 `model_migrations`、`memory_eval_reports`。
- 测试：新增 `tests/test_model_migration.py`（10 项）、`tests/test_memory_evaluation.py`（6 项）、`tests/test_drift.py`（9 项）；Python 总数 187 → 212，客户端保持 29。
- 状态变化：新增 MD-08、MD-09、MM-11、MM-12、DG-10 ✅。
- 已知限制：迁移回归使用内置样本集，未接入外部评测平台；漂移检测基于事件窗口与任务分布，不读取主机指标；记忆评测内置用例规模有限。

### 2026-10-20 — 项目初始交付 + 八轮迭代汇总

按 `progress.md` Session 1–8 汇总的当前状态：

| 轮次 | 主题 | 结果 |
|---|---|---|
| Session 1 | 服务端核心全链路 + Android 骨架 + 文档 | 51 项 Python 测试 |
| Session 2 | 诊断闭环（案例/多假设/探针）+ 真实模型 Planning/意图分类 | 75 项 |
| Session 3 | 离线队列/幂等同步 + Android 无障碍 + 原生壳修复 | 83 + 27 |
| Session 4 | 执行内核接线（计划驱动、预算计量、路由、暂停恢复） | 92 + 27 |
| Session 5 | 控制面/工具/工作流/记忆/路由/诊断补强 + 客户端状态面板 | 123 + 27 |
| Session 6 | 真机/模拟器实测、真实模型联调、无障碍实测修复 | 123 + 29 |
| Session 7 | 上下文治理 + 评测完整性（保留集/安全套件/fail-closed 门禁） | 135 + 29 |
| Session 8 | 九级预算 + 成本报告 | 149 + 29 |
| Session 9 | 全量功能记录文档（`FEATURES.md`） | 149 + 29 |
| Session 10 | 十维收益 + 发布后监测与自动回滚判定 | 170 + 29 |
| Session 11 | 日志四层分层 + 扩展指标 + 分级告警 | 187 + 29 |
| Session 12 | 模型迁移执行链 + 记忆评测 + 环境漂移检测 | 212 + 29 |
| Session 13 | 运行回放 + 三库备份恢复 + 管理端面板 | 225 + 29 |
| Session 14 | 工具替代安全链 + 成功任务工作流抽取 | 246 + 29 |
| Session 15 | 真实模型复用 Claude 全局配置 + 分类/领域修正 | 255 + 29 |
| Session 13 | 任务运行回放 + 备份与恢复 + 管理端操作面板 | 225 + 29 |

功能状态以本文件 §2–§9 为准；每轮细节与缺陷修复见 `progress.md`。

### 2026-10-20 — 日志分层 + 可观测指标与告警

- 需求：PRD 37-40 / 86；Arch 9.2 / 20.1-20.2。
- 变更：四层日志（运行事件/执行轨迹/诊断证据/审计）独立存储与保留期；执行轨迹按任务连续 `step` 记录；证据导出需 `standard` + 授权；扩展模型/工具/ReAct/恢复/上下文/记忆/进化/回滚/意图维度指标；8 条分级告警规则；管理接口与 CLI。
- 代码：`core/log_layers.py`（`LayerPolicy`、`ExecutionTracer`、`EvidenceStore`、`LayerManager`）、`core/alerts.py`（`AlertPolicy`、`evaluate_alerts`）、`models/metering.py`（`MeteredModel`）、`core/metrics.py`（扩展快照/Prometheus）、`core/events.py`（`aggregate`/`count`/`purge_before`/`delete_task_events`）、`core/persistence.py`（两张新表及方法）、`api/server.py`、`cli.py`、`app.py`、`core/controller.py`、`react/engine.py`。
- 接口/配置：`GET /alerts`、`/logs/layers`、`/logs/traces`、`/logs/evidence`；`POST /logs/purge`、`/logs/delete-task`；CLI `logs`、`alerts`；新表 `execution_traces`、`diagnostic_evidence`。
- 测试：`tests/test_observability.py`（17 项），Python 总数 170 → 187，客户端保持 29。
- 状态变化：CP-04 扩展；CP-08 🔹 → ✅；新增 CP-09…CP-15 ✅；未实现清单移除「日志分层细化」「意图级指标」。
- 已知限制：执行轨迹为事件级，不含逐 token/屏幕录制；证据引用需外部存储配合；清理仅按保留期，不做容量上限。

### 2026-10-20 — 十维收益评价 + 发布后监测

- 需求：PRD 50 / 82；Arch 11.5 / 12.3。
- 变更：新增十维收益向量与跨领域回归判定；新增发布后监测（时间窗采样、上窗对比、自动回滚）并接入候选生命周期。
- 代码：`evolution/benefit.py`（`evaluate_benefit`）、`evolution/monitor.py`（`MonitorPolicy`、`collect_metrics`、`PostReleaseMonitor`）、`evolution/evaluator.py`（门禁跨领域检查）、`evolution/controller.py`（`run_evaluation` / `monitor`）、`core/controller.py`（任务 `source` 标记）、`evolution/runner.py`、`api/server.py`、`cli.py`。
- 接口/配置：`monitor` 无指标时自动采集；`MonitorPolicy` 阈值可覆盖；候选新增 `benefit_result` / `monitor_result` / `deployed_at` / `target_domain`。
- 测试：`tests/test_evolution_benefit.py`（21 项），Python 总数 149 → 170，客户端保持 29。
- 状态变化：EV-03、EV-08、RL-06 🔹 → ✅；新增 EV-12 ✅。
- 已知限制：长期收益需 ≥20 条发布后样本；稳定性为失败证据代理；跨领域需至少两个领域数据。

### 2026-10-20 — 建立全量功能记录

- 新增本文件，作为功能维护的单一入口；`README.md` 增加链接。
- 测试基线：Python 149、客户端 29。
