# EvoAgent 全量总体架构与 PRD 正式版
**正式设计基线 1.0｜2026-10-20**

## 1. 产品定义
EvoAgent 是 Android 手机优先、本地优先的个人通用智能体系统。用户通过文字/语音提出目标；系统解析意图、约束与风险，选择领域专家和执行路径，优先复用稳定工作流，必要时进行受限动态规划；工具动作经过统一权限、预算、Schema 和副作用控制；结果经过独立验收，运行证据用于诊断、候选改进、独立评测、受控发布和回滚。

“无限进化”在工程上指持续、可验证且有预算的改进循环，不代表无限权限或无边界自修改。成功标准是长期提升真实任务成功率、质量、恢复率和资源效率。

## 2. 正式架构决策
1. Android 手机是第一正式终端；Windows、平板和其他桌面设备为后续扩展，不是首发前置依赖。
2. 端侧与运行宿主解耦。Android 负责交互、审批、原生设备桥接、本地日志、离线能力、更新校验和诊断导出。
3. 复杂模型路由、长任务、全局记忆治理、进化评测与发布可由远端服务或用户自有宿主承担；远端不是关键状态持久化的唯一位置。
4. 采用受保护共享内核与独立领域专家。专家拥有私有工具、技能、工作流、记忆、提示策略和评测，但不得覆盖全局安全、权限、Task of Record 或发布门禁。
5. 稳定执行轨道与候选进化轨道隔离。候选必须通过独立评测和发布控制器，不能自我批准。
6. Task of Record 是单次任务权威事实源；Goal、Task、Node、Sync、Asset、Approval 和 Release 使用独立状态机。
7. 模型只提出动作，所有外部副作用经过 Tool Gateway/Action Guard。
8. 事件、检查点和副作用账本分离：日志用于追溯，检查点用于恢复，副作用账本用于对账和防重复。
9. 上下文压缩引擎可共享，但记忆命名空间、隐私、保留和领域策略隔离。
10. 进化必须证明真实收益；一次成功只形成候选，不自动晋升稳定能力。

## 3. 总体逻辑架构
```text
Android Phone (first-class client)
  ├─ Interaction / Voice / Approval UI
  ├─ Local Task State / Event Store / Checkpoints
  ├─ Native Device Bridge / Action Guard
  ├─ Offline Capability Runtime
  ├─ Sync Coordinator / Release Verifier / Rollback
  └─ Diagnostics Export
          │ authenticated versioned protocol
          ▼
EvoAgent Host / Backend (optional personal host or remote service)
  ├─ Unified API / Identity / Policy Gate
  ├─ Task Orchestrator + Intent Router + Planner
  ├─ Workflow/DAG Runtime + Agent Runtime Adapter
  ├─ Model Gateway + Tool/Capability Gateway
  ├─ Memory & Context Governance
  ├─ Event/Trace + Recovery Coordination
  ├─ Diagnostics + Evolution Controller
  ├─ Independent Evaluation/QEEG
  └─ Promotion/Release/Distribution Controller
```

## 4. 八个逻辑服务
- **SV-01 任务编排：** Task/Run/Node 状态、路由、Planning、ReAct、DAG、取消和恢复。
- **SV-02 模型网关：** 供应商适配、模型选择、请求格式、超时/限流和成本对账。
- **SV-03 能力与工具：** Tool/Skill/Workflow/Expert/Capability Pack 注册、契约和依赖。
- **SV-04 记忆与上下文：** 命名空间、检索、冲突、ContextManifest、压缩和来源追踪。
- **SV-05 同步与观测：** 事件接收、幂等同步、版本关联、健康/回滚反馈。
- **SV-06 进化与诊断：** 问题案例、根因假设、候选提案和实验预算。
- **SV-07 评测与发布门禁：** 独立质量、安全、兼容和成本评测。
- **SV-08 版本分发：** 策略/工作流/能力包/模型档案/客户端制品的签名分发、灰度和回滚。

八个逻辑服务不意味着必须拆成八个微服务。个人部署优先模块化单体，只有负载、故障隔离或安全边界有证据时才拆分。

## 5. 四个受保护控制器
- **CTRL-01 Runtime Controller：** 执行状态、工具授权、预算、取消和恢复。
- **CTRL-02 Evolution Controller：** 候选实验、诊断队列、实验预算与停止条件；不拥有最终发布权。
- **CTRL-03 Evaluation Gate Controller：** 独立质量/安全/兼容性判定；候选不能修改门槛。
- **CTRL-04 Release Controller：** 正式版本指针、分发、灰度、晋级、暂停和回滚。

## 6. 任务执行模式
- 稳定工作流：适合重复且可验收任务，先检查输入、权限、依赖、版本和预算。
- 混合 Agent 工作流：主流程确定，局部节点需要动态判断。
- 动态规划：没有适用工作流时，在工具白名单、最大步数、时限、预算和停止条件内探索。

Router 决定候选路径；Planner 生成计划；DAG Runtime 管节点状态；ReAct 只做受约束局部循环；Action Guard 拥有动作允许/拒绝的最终策略权；Evaluator 独立验收。

## 7. 产品功能全景
- 自然语言/语音目标输入、必要问题澄清、任务历史、取消、审批、结果与部分完成展示。
- 多层意图路由、领域专家、模型档案和工具契约。
- 稳定工作流复用、轨迹到候选工作流编译、依赖图和资产生命周期。
- 用户/项目/领域/会话/任务分层记忆，动态上下文、压缩、可信度与来源追踪。
- 运行事件、故障分类、工具健康、独立诊断、QEEG 评测和回归案例库。
- AGIG 长期目标治理、主动机会评估、APRO 资源调度、预算和停止控制。
- Android 受控设备操作、本地事件队列、离线诊断包、可靠同步、版本校验和回滚。
- 端云协同进化、隔离实验、独立门禁、签名发布、灰度和线上收益反馈。

## 8. 版本与可复现
每次运行固定绑定不可变 Runtime Manifest，至少记录 App/Core、Runtime Adapter、模型档案、Router/Planner、Context Policy、专家、工作流、工具适配器、评测集和环境摘要。运行中默认不热切换组件版本；升级需明确恢复/迁移策略。

## 9. 产品非目标与硬边界
- 不允许模型直接执行未注册工具。
- 不允许候选修改权限、安全规则、评测门槛或稳定版本指针。
- 不允许将一次成功轨迹直接作为稳定工作流。
- 不允许未经授权上传原始对话、截图或敏感文件。
- 不允许在副作用状态未知时盲目重放。
- 不允许主动目标无限递归或超预算运行。
- 不允许将 Mock 结果声明为真实供应商能力。

## 10. 验收总原则
任务成功必须由产物、结构、语义质量、外部状态或用户确认等证据证明；“模型说完成”不是充分证据。每个正式能力均需版本、依赖、权限、评测、适用范围和回滚信息。
