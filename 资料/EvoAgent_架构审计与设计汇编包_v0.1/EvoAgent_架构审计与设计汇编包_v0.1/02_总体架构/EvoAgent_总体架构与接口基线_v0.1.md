# EvoAgent 总体架构与接口基线
**版本：** Architecture Baseline Draft v0.1  
**日期：** 2026-10-21  
**用途：** V0.1 工程实施与后续专项合并的统一基线  
**状态：** 初步统一稿；需结合完整专项原稿进行第二轮逐项核对

## 1. 产品定义

EvoAgent 是本地优先的个人通用智能体系统。用户通过文字、语音或 API 提出目标；系统解析意图与约束，路由到合适专家，匹配稳定工作流或受控动态规划，执行工具和 Agent 节点，验证结果并记录运行证据；通过独立诊断、评测、发布与回滚机制持续改进质量、效率和稳定性。

长期价值不是“修改自己次数更多”，而是：
- 更可靠地完成真实任务；
- 在适用场景复用经过验证的能力；
- 更准确地诊断失败；
- 在相同预算下提高成功率/质量或降低成本/时延；
- 保持可追溯、可恢复、可停止与用户可控。

## 2. 系统原则

1. 任务结果优先于模型输出。
2. 确定性工具优先于不必要的自由推理。
3. 已验证工作流优先复用；没有适用工作流才动态规划。
4. 一次成功只生成候选，不自动变成稳定能力。
5. 运行时、诊断器、评测器和发布控制器职责隔离。
6. 模型输出是不可信提议，不能直接取得工具执行权。
7. 记忆与上下文共享策略，不共享无边界内容。
8. 所有关键动作可追踪、版本可复现、发布可回滚。
9. 本地离线是基础设计约束，不是异常状态。
10. 自主目标必须有预算、授权、停止条件与验收证据。

## 3. 统一架构

```text
[Android / Desktop / CLI / API]
             |
       Interaction Gateway
             |
 [Intent + Goal + Risk + Authorization]
             |
          [Router]
             |
   [Workflow Match / Planner]
       /             \
 Stable Workflow   Dynamic Plan
       \             /
        [DAG Runtime]
             |
  [ReAct / Agent Runtime Adapter]
             |
 [Tool Gateway + Action Guard + Budget]
             |
 [Tools / Local OS / External APIs / Models]
             |
 [Verifier + Acceptance Contract]
             |
 [Events + Checkpoints + Side-effect Ledger]
             |
 [Diagnostics + QEEG + Asset Governance]
             |
 [Candidate Experiment / Promotion Gate]
             |
 [Immutable Release / Observe / Rollback]
```

横切能力：Context & Memory Manager、Policy/TSEG、APRO Resource Scheduler、RERA Recovery Manager、GECO Event Store、KCLG Asset Registry、AGIG Goal Governance、HACCG user control surface（缩写待核验）。

## 4. 模块职责

### 4.1 Interaction Gateway
负责接收文字/语音/API 请求、显示任务状态、收集审批和交付结果。不能绕过后端策略直接执行高风险动作。

### 4.2 Intent Router
将输入转化为结构化目标、约束、领域候选、风险标签和所需能力。输出应包含选择理由、缺失信息和置信状态；不确定时允许追问或使用通用路径。

### 4.3 Goal Governance（AGIG）
管理显式目标、长期目标、派生子目标、维护目标和主动机会。负责价值评估、授权前置检查、优先级建议、目标状态、预算/截止/停止条件。它不负责 DAG 节点执行。

### 4.4 Planner
把目标转化为计划，优先匹配已有工作流；否则创建受限 DAG 或 Agent 节点。不能扩大授权范围，也不能自行批准高风险动作。

### 4.5 Workflow/DAG Runtime
管理节点依赖、分支、并发、重试、超时、检查点、补偿和节点状态。计划执行与长期目标状态分别持久化。

### 4.6 ReAct Runtime
执行局部动态循环：观察 → 选择一个动作 → 经 Action Guard 校验 → 执行 → 观察 → 验收/继续/停止。每轮受步数、时间、预算和工具白名单约束。

### 4.7 Tool Registry / Tool Gateway
每个工具有 ID、版本、输入/输出 Schema、权限、风险等级、超时、幂等策略、成本估算、健康状态和 contract tests。模型不能直接调用未注册工具。

### 4.8 Expert Registry
每个专家声明领域、能力、工具、技能、工作流、记忆命名空间、模型偏好、评测集和版本。领域专家不得直接修改全局安全与发布门禁。

### 4.9 Context & Memory Manager
按用户、项目、领域、会话和任务管理不同上下文；构建当前模型上下文时进行检索、压缩、去重和冲突处理。摘要保留来源引用、更新时间和不确定性。

### 4.10 GECO / Event Store
记录任务、运行、节点、工具、模型、版本、成本、错误、恢复和验收事件。事件先本地持久化；日志本身不作为可信命令。

### 4.11 RERA / Recovery Manager
对超时、网络错误、工具失败、状态不确定和部分完成进行分类，决定重试、查询外部状态、补偿、请求用户确认或停止。对非幂等外部操作不得盲目重放。

### 4.12 APRO / Resource Scheduler
管理模型、并发、CPU/内存、费用和后台任务配额。保护前台交互和必要安全维护；不突破用户预算。

### 4.13 Diagnostics
从证据形成根因假设与可验证改进建议。必须区分模型、规划、工具、适配器、供应商、网络、权限、环境和验收失败；诊断不直接修改稳定生产资产。

### 4.14 QEEG / Evaluator
对任务结果、候选技能、工作流和模型档案进行独立验收。测试集、门槛和基线由受保护机制管理，候选不得自行修改。

### 4.15 KCLG / Asset Governance
管理知识、技能、工具、工作流、专家、评测集和版本资产；维护来源、生命周期、依赖图、适用范围、重复检测、过期与退役策略。

### 4.16 SELCIA / Promotion & Release Controller
把通过评测且获准的候选转成不可变发布版本；管理签名/摘要、兼容范围、灰度、健康观察、晋级和回滚。缩写正式释义需与原专项核验。

### 4.17 TSEG / Policy Gate
统一风险、权限、隐私、授权和动作策略。高风险动作按具体对象、范围、效果和预算检查；不接受由模型自我授予权限。

### 4.18 AGIG Opportunity Detector
从运行失败、质量退化、成本异常、资产缺口和用户长期目标发现机会。机会不是已授权任务，必须经价值、证据和权限检查。

## 5. 控制权规则

- Router 决定候选领域与执行路径，不负责工具执行。
- Planner 决定计划，不负责批准权限。
- DAG Runtime 拥有节点状态的唯一写入权。
- Goal Registry 拥有长期目标状态的唯一写入权。
- Action Guard/TSEG 拥有动作允许/拒绝的最终策略权。
- RERA 拥有恢复策略编排权，但不得覆盖用户取消或安全拒绝。
- APRO 决定资源分配，不改变目标的授权范围。
- QEEG 判断质量门槛是否通过，不批准自身门槛变更。
- SELCIA 管理正式版本指针，不生成自己的质量证据。
- KCLG 管理资产元数据和生命周期，不绕过发布门禁。
- GECO 记录事实，不作为状态恢复的唯一依据。
- 用户取消/权限撤销优先于普通规划和后台进化。

## 6. 核心数据契约

### 6.1 Task
```json
{
  "task_id": "uuid",
  "goal_id": "uuid",
  "project_id": "optional",
  "origin": "user|scheduled|derived|opportunity",
  "objective": "string",
  "constraints": {},
  "acceptance_contract_ref": "id/version",
  "risk_class": "low|medium|high|critical",
  "authorization_ref": "policy-id/version",
  "budget_ref": "budget-id",
  "status": "created|classified|planned|running|waiting_approval|recovering|verifying|succeeded|failed|cancelled|needs_user",
  "runtime_manifest_id": "immutable-id",
  "created_at": "RFC3339",
  "updated_at": "RFC3339"
}
```

### 6.2 Runtime Event
```json
{
  "event_id": "uuid-or-ulid",
  "task_id": "uuid",
  "run_id": "uuid",
  "sequence_no": 1,
  "event_type": "task.started|tool.completed|task.failed",
  "schema_version": "1.0",
  "runtime_manifest_id": "immutable-id",
  "created_at": "RFC3339",
  "privacy_level": "public|internal|sensitive|restricted",
  "payload_ref": "local-uri-or-inline-small-payload",
  "sync_status": "pending|in_flight|acknowledged|quarantined"
}
```

### 6.3 Tool Contract
```json
{
  "tool_id": "string",
  "version": "semver-or-immutable-revision",
  "input_schema": {},
  "output_schema": {},
  "risk_class": "low|medium|high|critical",
  "required_permissions": [],
  "timeout_seconds": 30,
  "idempotency": "idempotent|keyed|non_idempotent",
  "cost_model": {},
  "health_check_ref": "optional",
  "contract_test_ref": "required"
}
```

### 6.4 Capability Asset
```json
{
  "asset_id": "stable-id",
  "asset_type": "knowledge|skill|tool|workflow|expert|model_profile|evaluation_suite",
  "namespace": "global|domain|project|task",
  "version": "immutable-version",
  "content_digest": "sha256",
  "source_refs": [],
  "dependency_refs": [],
  "compatibility": {},
  "lifecycle_state": "candidate|validating|approved|active|degraded|deprecated|retired",
  "evaluation_evidence_refs": [],
  "rollback_ref": "optional"
}
```

### 6.5 Goal
```json
{
  "goal_id": "uuid",
  "goal_type": "explicit|long_term|derived|maintenance|opportunity",
  "parent_goal_id": "optional",
  "success_criteria": [],
  "authorization_ref": "policy-id/version",
  "budget_ref": "budget-id",
  "deadline": "optional",
  "expiry_policy": "condition",
  "status": "proposed|ready|running|waiting|paused|blocked|review|completed|cancelled|expired",
  "evidence_refs": []
}
```

## 7. 状态机边界

- Task State：描述单次任务。
- Node State：描述 DAG 节点。
- Goal State：描述长期目标。
- Sync State：描述事件同步。
- Asset Lifecycle：描述能力资产生命周期。
- Release State：描述版本发布、激活、健康观察与回滚。
- Approval State：描述某个具体动作的审批结果。

不同状态机通过 ID 和事件关联，不共用一个万能 `status` 字段。

## 8. 版本与复现

每次任务固定绑定不可变 Runtime Manifest，至少包括：
- EvoAgent Core 版本；
- Runtime/Harness Adapter 版本；
- 模型档案版本；
- Router/Planner/Prompt 配置版本；
- Tool Adapter 版本；
- Workflow/Skill/Expert 版本；
- Context Policy 版本；
- Evaluation Suite 版本；
- 环境/依赖摘要。

正式版本不得原地覆盖；变更生成新版本。候选、稳定与退役版本有明确引用，历史任务不自动漂移到最新版本。

## 9. 离线与同步

- 关键事件先写入本地事务存储。
- 网络恢复后采用至少一次传输与幂等去重。
- 服务端 ACK 仅在持久化后返回。
- 部分成功明确返回已确认/未确认 ID。
- 附件单独上传并校验摘要。
- 诊断包导入隔离、校验、脱敏并审计。
- 同步不等于恢复；检查点与副作用账本独立。
- 版本分发与事件补传分离，互不阻塞。

## 10. 评测与发布门禁

最低评测维度：
- 任务成功率与验收质量；
- 工具调用正确性；
- 恢复成功率；
- 成本与时延；
- 安全/权限违规；
- 兼容性与回归；
- 失败注入结果；
- 运行环境与测试集可复现性。

V0.1 默认不自动发布代码改动。先完成候选评测和人工批准；后续仅对明确 allowlist 的低风险配置开放自动晋级。

## 11. V0.1 工程边界

### Must
- Python 模块化单体；
- SQLite + 文件系统；
- 任务状态机、Tool Registry、Action Guard；
- 受控 ReAct；
- 一个端到端任务场景；
- Mock Runtime；
- 结构化事件；
- 最小检查点、取消、幂等与副作用状态；
- 结果验收、失败分类和有限恢复；
- 单元/集成/回归测试；
- 配置、启动、故障排查和可复现演示。

### Later
- DAG 工作流平台；
- 专家注册与多领域路由；
- 完整记忆治理和跨会话检索；
- 主动目标 AGIG；
- 隔离进化实验；
- Android 受控操作；
- 版本分发和双端同步；
- AI 漫剧等复杂能力包；
- 低风险自动发布。

### Not allowed
- 模型直接执行未注册工具；
- 自主修改安全门禁；
- 候选直接写入稳定生产资产；
- 不确定外部副作用盲目重试；
- 未经授权上传原始对话/截图；
- 将单次成功当成已验证能力。

## 12. 冻结与变更流程

1. 新需求登记为 RFC/ADR。
2. 指定受影响模块、接口、数据契约和兼容范围。
3. 检查是否破坏稳定内核或领域隔离。
4. 增加测试与迁移/回滚方案。
5. 对候选版本运行独立评测。
6. 审批通过后生成不可变版本。
7. 灰度/观察并保留回滚。
8. 更新架构索引与专项状态台账。

## 13. 未决事项

- AGIG/APRO/RERA/QEEG/KCLG/TSEG/SELCIA/GECO/HACCG 等缩写的正式名称、边界和责任人；
- 本地宿主优先的首发平台顺序；
- Android 是否作为首发 UI 还是第二阶段 UI；
- 真实模型供应商和离线模型范围；
- 本地沙箱选型与跨平台安全边界；
- 专家/技能/工作流的正式资产 Schema；
- 记忆删除、来源传播和隐私保留策略的详细协议；
- 自动发布的第一批 allowlist 对象；
- 50+ 专项原稿逐项核对与版本溯源。

## 14. 结论

本基线优先冻结职责、控制权、核心状态、数据契约、安全边界和本地运行闭环。领域能力和工作流内容保持可扩展。所有专项原稿并入时，若与本基线冲突，必须记录来源、决策时间、适用部署分支和最终取舍，不得静默覆盖。
