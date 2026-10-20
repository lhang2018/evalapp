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

测试基线（2026-10-20）：**Python 149 项 + 客户端 29 项全部通过**。

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
| DG-09 | LLM 辅助假设生成 | ⬜ | — | 按 Arch 9.4 模型不判根因 |

### 2.10 进化与评测（PRD 46-54 / Arch 10-11）

| ID | 功能 | 状态 | 代码位置 | 说明 / 限制 |
|---|---|---|---|---|
| EV-01 | 候选生命周期状态机 | ✅ | `evolution/candidates.py`, `evolution/controller.py` | 与 Arch 10.4 一致 |
| EV-02 | 候选提案/升级 | ✅ | `evolution/controller.py` | API/CLI 全通 |
| EV-03 | 十维收益（当前质量/成本/时延） | ✅ | `evolution/candidates.py` | 其余维度占位 🔹 |
| EV-04 | 评测集元数据/版本/适用范围 | ✅ | `evolution/datasets.py` | |
| EV-05 | 独立保留集 | ✅ | `evolution/datasets.py`, `evolution/controller.py` | 只用于门禁决策 |
| EV-06 | 基线与候选对照 | ✅ | `evolution/controller.py`, `evolution/evaluator.py` | 基线数据/版本不等价直接拒绝 |
| EV-07 | 确定性安全套件 | ✅ | `evolution/safety.py` | 风险拒绝/确认权威/硬预算/隐私四类检查 |
| EV-08 | 发布门禁 ReleaseGate | ✅ | `evolution/evaluator.py` | 硬难度回归、约束违规、工具失败率、恢复成功率、安全 fail-closed |
| EV-09 | 评测报告持久化 | ✅ | `core/persistence.py` | `eval_reports` 含元数据 |
| EV-10 | 候选实验预算 | ✅ | `evolution/controller.py`, `evolution/runner.py` | 双记账 `candidate:<id>` + `evolution`；耗尽记失败不中断批次 |
| EV-11 | 候选代码隔离沙箱 | ⬜ | — | PRD 47.4 / L3 |

### 2.11 发布管理（PRD 59-63 / 80-82）

| ID | 功能 | 状态 | 代码位置 | 说明 / 限制 |
|---|---|---|---|---|
| RL-01 | 发布清单 | ✅ | `release/manager.py` | |
| RL-02 | 签名与验证 | ✅ | `release/manager.py` | `EVOAGENT_RELEASE_SECRET` |
| RL-03 | 灰度接口 | ✅ | `release/manager.py` | |
| RL-04 | 回滚 | ✅ | `release/manager.py`, `api/server.py`, `cli.py` | `POST /releases/rollback` |
| RL-05 | L0-L4 风险分级发布规则 | ✅ | `evolution/controller.py` | 核心安全边界改动禁止普通发布 |
| RL-06 | 发布后监测接指标源 | 🔹 | `release/manager.py` | 接口已留 |
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
| CP-04 | 指标 | ✅ | `core/metrics.py` | JSON + Prometheus（`?format=prometheus`） |
| CP-05 | 管理页数据接口 | ✅ | `api/admin.py` | `GET /admin`（HTML） |
| CP-06 | 健康检查 | ✅ | `api/server.py`, `app.py` | 含专家、预算、诊断计数 |
| CP-07 | Web 管理端完整界面 | ⬜ | — | 数据层已备 |
| CP-08 | 日志分层细化 | 🔹 | `core/events.py` | 当前统一事件模型 |

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
| `/experts` | 专家与能力图 |
| `/metrics` | 指标（JSON；`?format=prometheus`） |
| `/audit` | 审计日志 |
| `/costs?limit=N` | 成本报告（totals/by_task/by_domain/by_model/evolution） |
| `/workflows`、`/workflows/{id}` | 工作流资产库 |
| `/memory/stats` | 记忆统计 |
| `/diagnostics`、`/diagnostics/{case_id}`、`/diagnostics/trends` | 诊断案例/详情/趋势 |
| `/evolution/candidates`、`/evolution/candidates/{id}`、`/evolution/datasets` | 进化候选与数据集 |
| `/releases`、`/releases/{id}` | 发布清单 |
| `/admin` | 只读管理页 |

### POST

| 路径 | 说明 |
|---|---|
| `/tasks` | 提交任务；可选 `client_request_id` 幂等（重复返回原 `task_id` + `202 deduplicated`） |
| `/tasks/{id}/cancel` | 取消 |
| `/tasks/{id}/pause` / `/tasks/{id}/resume` | 暂停/恢复（`409 requires_force` 表示需显式 force） |
| `/events/sync` | 批量事件摄取 → `accepted` / `duplicates` / `rejected` |
| `/evolution/candidates` | 候选提案 |
| `/evolution/candidates/{id}/evaluate` | 评测 |
| `/evolution/candidates/{id}/approve` | 批准 |
| `/evolution/candidates/{id}/deploy` | 发布 |
| `/evolution/candidates/{id}/monitor` | 监测 |
| `/evolution/candidates/{id}/rollback` | 回滚 |
| `/releases/rollback` | 发布回滚 |
| `/diagnostics/scan` | 扫描失败任务生成案例 |
| `/diagnostics/{case_id}/experiments` | 运行探针实验（可选 `fault: error|timeout|fail`） |
| `/diagnostics/{case_id}/candidates` | 假设升级候选 |

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
| `serve [--port N]` | 启动 REST API（默认 8300） |
| `candidates` / `propose` / `evaluate` / `approve` / `deploy` / `monitor` | 进化候选操作 |
| `workflows` | 工作流资产库 |
| `budget` | 全部预算层级限额 + 运行用量 |
| `costs` | 成本报告 |
| `releases` / `release-rollback` | 发布清单/回滚 |

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
| `client_requests` | 任务幂等键 → task_id |
| `runtime_events` | 运行事件（含隐私/关联/同步字段） |
| `memory_items` | 记忆、关系、使用计数 |
| `tool_executions` | 幂等执行日志 |
| `budget_ledger` | 预算账本（scope + 日/月键） |
| `diagnostic_cases` / `diagnostic_hypotheses` / `diagnostic_experiments` | 诊断 |
| `evolution_candidates` | 候选生命周期 |
| `eval_reports` | 评测报告（含元数据） |
| `release_manifests` / `release_events` | 发布清单与事件 |
| `audit_log` | 审计 |

兼容策略：旧库启动时按 `PRAGMA table_info` 自动补列（如 `runtime_events`）；结构不兼容时重建对应表。

---

## 7. 配置项

`evoagent/config.py`。环境变量：

| 变量 | 说明 |
|---|---|
| `EVOAGENT_DATA` | 数据目录 |
| `EVOAGENT_PROVIDER` | `mock`（默认）/ `openai` |
| `EVOAGENT_API_KEY` | 模型 Key（不得写入仓库或文档） |
| `EVOAGENT_BASE_URL` | OpenAI 兼容端点（如 DeepSeek） |
| `EVOAGENT_MODEL` | 模型名 |
| `EVOAGENT_HIGH_RISK` | 高风险策略：confirm（默认）/ deny |
| `EVOAGENT_API_TOKEN` | 普通接口 Bearer |
| `EVOAGENT_ADMIN_TOKEN` | 管理接口 Bearer |
| `EVOAGENT_RELEASE_SECRET` | 发布签名密钥 |

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

---

## 8. 测试矩阵

### Python（`tests/`，149 项）

| 文件 | 覆盖范围 |
|---|---|
| `test_state_machine.py` | 状态机合法/非法迁移 |
| `test_budget.py` | 基础预算 |
| `test_budget_hierarchy.py` | 九级预算、池、候选双记账、成本报告（13 项） |
| `test_runtime.py` | 端到端运行时 |
| `test_control.py` | 计量、限额、计划持久化、路由、暂停恢复、重跑、副作用闸门（9 项） |
| `test_planning.py` | 计划校验/回退、意图分类 |
| `test_tools.py` | 契约、权限、副作用、幂等 |
| `test_tool_workflow.py` | 工具超时/重试/输出契约、工作流并行/跳过/续跑 |
| `test_memory.py` | 记忆存储、替代、检索 |
| `test_memory_router_diag.py` | 记忆关系、路由评分、诊断趋势/故障注入 |
| `test_workflow.py` | DAG 引擎 |
| `test_diagnostics.py` | 案例/假设/探针/升级 |
| `test_evolution.py` | 候选生命周期、评测、门禁 |
| `test_governance.py` | 上下文治理、评测完整性、fail-closed 门禁（12 项） |
| `test_event_sync.py` | 事件同步、旧库迁移、幂等提交（8 项） |
| `test_control_plane.py` | 鉴权、审计、指标、管理页 |
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
| LLM 辅助诊断假设 | PRD 41-45 / Arch 9.4 | ⬜ | 模型不参与根因判定 |
| 候选代码隔离沙箱 | PRD 47.4 / L3 | ⬜ | 评测执行器运行受控任务，不是任意代码沙箱 |
| 视觉/OCR/语音操作 | PRD 64/68 | ⬜ | 仅无障碍节点与显式 Intent |
| 后台常驻同步 | PRD 72-76 | ⬜ | 前台 + 手动重试 |
| 端到端加密、多设备冲突合并 | PRD 72-76/86 | ⬜ | |
| Web 管理端完整界面 | PRD 90 | ⬜ | `GET /admin` 只读页与数据接口已备 |
| 发布自动分支/合并 | PRD 59-63 | ⬜ | 仅 git 基线 |
| 十维收益中其余维度 | PRD 50 | 🔹 | 质量/成本/时延已可用 |
| 发布后监测接指标源 | PRD 80-82 | 🔹 | 接口已留 |
| 日志分层细化 | PRD 37-40 | 🔹 | 统一事件模型 |
| 任务模型全字段 | PRD 7.3 | 🔹 | 核心子集已实现 |
| 意图级指标 | PRD 37-40 | 🔹 | 现有任务/领域/模型维度 |

---

## 10. 验证步骤

```bash
# 1. 服务端测试
python -m pytest tests/ -q                       # 149 passed

# 2. 客户端测试 + 类型检查
cd android-app && npm run test:offline && npm run typecheck && cd ..

# 3. 无 Key 全链路
python -m evoagent.cli demo
python -m evoagent.cli workflow
python -m evoagent.cli budget
python -m evoagent.cli costs

# 4. API 冒烟
python -m evoagent.cli serve --port 8300
#   - 两次相同 client_request_id 的 POST /tasks → 同一 task_id
#   - 重复 POST /events/sync → duplicates
#   - GET /costs /metrics /admin

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

功能状态以本文件 §2–§9 为准；每轮细节与缺陷修复见 `progress.md`。

### 2026-10-20 — 建立全量功能记录

- 新增本文件，作为功能维护的单一入口；`README.md` 增加链接。
- 测试基线：Python 149、客户端 29。
