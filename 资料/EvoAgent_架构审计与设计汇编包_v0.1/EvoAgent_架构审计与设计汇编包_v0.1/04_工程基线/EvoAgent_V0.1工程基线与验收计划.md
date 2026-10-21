# EvoAgent 工程基线与 V0.1 实施验收
**版本：** Engineering Baseline Draft v0.1

## 1. V0.1 目标

在单机环境跑通一个受控任务的完整闭环：
创建任务 → 解析目标 → 选择工具/计划 → 执行一个或多个步骤 → 校验工具输出 → 最终验收 → 结构化记录 → 失败分类 → 有限恢复或安全停止。

V0.1 不以“所有模块都存在”为成功标准，而以一个端到端场景可运行、可测试、可复现为标准。

## 2. 推荐工程结构

```text
evoagent/
  apps/
    desktop/
    android/                 # 后续/并行交互端
    cli/
  core/
    task_service.py
    goal_registry.py
    intent_router.py
    planner.py
    react_runtime.py
    state_machine.py
    checkpoint.py
    side_effect_ledger.py
    action_guard.py
    recovery.py
  runtime/
    adapter.py
    mock_runtime.py
    model_adapter.py
  tools/
    registry.py
    contracts.py
    adapters/
  workflows/
    definitions/
    schema/
    compiler.py
    validator.py
    runner.py
    versioning.py
  context/
    manager.py
    memory_namespaces.py
    summarizer.py
    retrieval.py
    policy.py
  assets/
    registry.py
    lifecycle.py
    dependency_graph.py
  observability/
    events.py
    trace_store.py
    diagnostics.py
    tool_health.py
  evaluation/
    datasets/
    regression.py
    quality.py
    benchmark.py
  evolution/
    opportunities.py
    experiments.py
    promotion_gate.py
    releases.py
  storage/
    database.py
    migrations/
    filesystem.py
  policies/
    authorization.yaml
    risk_rules.yaml
    budgets.yaml
  tests/
    unit/
    integration/
    regression/
    failure_injection/
  docs/
    PRD.md
    ARCHITECTURE.md
    ADR/
```

## 3. 建议核心技术

- Python 3.11+：先以稳定可用的版本为准，锁定依赖。
- SQLite：本地事务和状态。
- JSONL：事件导出与诊断。
- YAML/JSON：工作流和策略配置。
- Git：工作流、提示词、技能、配置和版本管理。
- pytest：单元、集成与回归。
- Pydantic 或 JSON Schema：数据契约验证（具体选型待 PoC）。
- Mock Runtime：无 API Key 时跑通系统行为测试。
- 模型供应商适配器：独立于核心运行时。
- Windows/Linux：核心能力保持可移植；Android 使用原生交互层与受控桥接。

不要在 V0.1 默认引入微服务、Kubernetes、Redis、复杂向量数据库或自主发布系统，除非有可验证的需求。

## 4. V0.1 最小数据表

- `projects`
- `goals`
- `tasks`
- `task_runs`
- `node_runs`
- `runtime_events`
- `runtime_manifests`
- `tool_registry`
- `approvals`
- `side_effect_ledger`
- `checkpoints`
- `evaluation_suites`
- `evaluation_runs`
- `asset_registry`
- `audit_log`

第一版可以按实际事务合并部分表，但逻辑职责不能丢失。

## 5. 必须完成的端到端测试

### T01 正常执行
给定明确目标，工具调用通过 Schema 和权限检查，结果满足验收契约，任务进入 succeeded。

### T02 工具参数错误
工具参数不符合 Schema，动作不执行，产生可定位事件并返回可解释错误。

### T03 权限拒绝
高风险工具未获批准时必须被阻断；模型不能绕过 Action Guard。

### T04 超时与恢复
工具超时后按策略查询状态、有限重试或安全停止；不能直接认定副作用未发生。

### T05 重复请求
相同幂等键不会产生重复逻辑任务或重复付费/外部副作用。

### T06 进程重启
重启后可以恢复任务状态；已确认完成的副作用不重复执行。

### T07 用户取消
停止创建新步骤，安全收尾正在执行的操作，保留审计记录。

### T08 预算耗尽
达到预算上限后停止进一步调用，明确报告已完成部分与剩余工作。

### T09 验收失败
工具成功但最终产物不满足质量契约，任务不能被标记为成功。

### T10 版本追溯
每次运行可以查询其 Runtime Manifest、模型、工具和工作流版本。

### T11 离线日志
网络不可用时事件仍本地保存并可导出；离线不等于事件丢失。

### T12 候选隔离
候选版本评测失败时，稳定版本与稳定指针保持不变。

## 6. 版本与发布策略

- 配置、工具、工作流、技能、专家、模型档案和评测集分别版本化。
- 每次运行绑定不可变版本清单。
- 生产任务不自动漂移到最新版本。
- V0.1 不自动发布代码改动。
- 候选通过测试也不自动等于有发布授权。
- 发布记录包含来源、基线、测试证据、适用范围和回滚目标。

## 7. 工程交付物清单

- 可运行工程；
- `README` 与安装/启动说明；
- `.env.example` 或安全配置示例（不含真实密钥）；
- 数据库迁移；
- Mock 模式；
- 至少一个真实模型适配器（可选配置）；
- 工具契约与示例工具；
- 单元、集成、回归测试；
- 一条端到端演示；
- 故障排查说明；
- 版本与配置清单；
- 明确的未实现能力表。

## 8. 通过标准

V0.1 只有在 T01-T12 的适用测试通过、所有高风险拒绝测试有效、无未解决的严重数据丢失/重复副作用问题、且能够从干净环境按文档复现时，才可标记为可验收基线。
