"""数据契约与枚举的唯一权威（协作基座 F-00.3）。

**动机**：契约字段分散在实现各处，同一概念有多套取值域 —— 本次审计发现
6 处契约与实现不一致（`privacy_level`、`sync_status`、`idempotency`、
L 级语义、Goal 状态、Asset namespace）。并行开发时各方各自定义枚举，
会把这 6 处分叉固化成 N 处。

**约定**：
1. 本模块只放**跨模块共享**的数据形状与枚举。单模块私有结构留在各自域内。
2. 特性代码**必须**从这里取枚举，禁止本地重复定义。
3. 新增取值时先改本模块（单一 owner 裁定），再在实现中消费。

**与设计文档的关系**：字段与取值以 `docs/.../09_接口数据契约与实施验收/
EvoAgent_核心接口与数据契约正式规范.md` 为准；§9 的 6 条文档内部冲突在
`CONFLICT_DECISIONS` 中记录代码级裁定。
"""

from typing import Dict, Final, Tuple

# ---------------------------------------------------------------------------
# 统一标识（契约 §1）
# ---------------------------------------------------------------------------

#: 运行实例 ID 的构造方式。注意当前实现为确定性派生（同一 task 恒定同值），
#: 而非独立执行实例 ID —— 见 IMPLEMENTATION 契约差异待办。
RUN_ID_IS_DERIVED: Final[bool] = True

#: 副作用账本状态机（契约 §8 / 01 卷 R-12）。当前实现尚无此实体，
#: 由 F-03 落地；此处先固定取值域，避免实现时再定义。
SIDE_EFFECT_STATUS: Final[Tuple[str, ...]] = (
    "planned", "submitted", "confirmed", "failed", "unknown", "compensated",
)

#: 副作用状态中禁止盲目重放的取值（ADR-006：unknown 必须先对账）。
SIDE_EFFECT_NO_BLIND_RETRY: Final[frozenset] = frozenset({"unknown"})

# ---------------------------------------------------------------------------
# Task 契约（契约 §2）
# ---------------------------------------------------------------------------

#: `Task.origin` 取值域（契约 §2）。
ORIGIN_VALUES: Final[Tuple[str, ...]] = (
    "user", "scheduled", "derived", "opportunity",
)

#: 契约 §2 定义的 Task.status 取值域。实现侧 TaskStatus 有 13 个值，
#: 与本表存在差异（实现缺 waiting_approval，多 READY/WAITING_DEPENDENCY/
#: SUSPENDED），属已知契约差异，裁定见 CONFLICT_DECISIONS。
TASK_STATUS_SPEC: Final[Tuple[str, ...]] = (
    "created", "classified", "planned", "running", "waiting_approval",
    "recovering", "verifying", "succeeded", "failed", "cancelled",
    "needs_user",
)

RISK_CLASS: Final[Tuple[str, ...]] = ("low", "medium", "high", "critical")

# ---------------------------------------------------------------------------
# Runtime Event 契约（契约 §3）
# ---------------------------------------------------------------------------

#: 契约 §3 的 `privacy_level` 四值。
#:
#: ⚠️ 冲突：实现侧 `core/privacy.py` 与 `core/events.py` 用的四值是
#: standard/sensitive/secret/private —— **数量相同、语义完全不同**。
#: 跨端对齐有误判风险。裁定见 CONFLICT_DECISIONS。
PRIVACY_LEVEL_SPEC: Final[Tuple[str, ...]] = (
    "public", "internal", "sensitive", "restricted",
)

#: 实现侧当前的隐私分级（`core/privacy.py::CLASSIFICATIONS`）。
#: 新代码请统一改用 PRIVACY_LEVEL_SPEC；此处保留仅供迁移期对照。
PRIVACY_LEVEL_LEGACY: Final[Tuple[str, ...]] = (
    "standard", "sensitive", "secret", "private",
)

#: 契约 §3 的 `sync_status` 四值。⚠️ 冲突：服务端只有 local/synced，
#: 客户端有 pending/in_flight/quarantined，**两端都缺 acknowledged**，
#: 当前靠回执的 confirmed 数组旁路表达。见 CONFLICT_DECISIONS。
SYNC_STATUS_SPEC: Final[Tuple[str, ...]] = (
    "pending", "in_flight", "acknowledged", "quarantined",
)

# ---------------------------------------------------------------------------
# Tool 契约（契约 §4）
# ---------------------------------------------------------------------------

#: 契约 §4 的 `idempotency` 三值。⚠️ 冲突：实现侧用的是
#: none/key/state-check（`tools/contract.py`）。见 CONFLICT_DECISIONS。
IDEMPOTENCY_SPEC: Final[Tuple[str, ...]] = (
    "idempotent", "keyed", "non_idempotent",
)

IDEMPOTENCY_LEGACY: Final[Tuple[str, ...]] = ("none", "key", "state-check")

# ---------------------------------------------------------------------------
# Capability Asset 契约（契约 §6）
# ---------------------------------------------------------------------------

#: 契约 §6 的 `asset_type` 八值。实现侧 `CapabilityEntry` 用 `domain` 字段
#: 承担了类似职责，但语义不同且无此枚举。
ASSET_TYPE: Final[Tuple[str, ...]] = (
    "knowledge", "skill", "tool", "workflow", "expert", "capability_pack",
    "model_profile", "evaluation_suite",
)

#: 契约 §6 的 `namespace` 五域（受保护内核 → 领域私有）。
#: ⚠️ 冲突：记忆侧 `memory/store.py::Scope` 是另一套五值
#: （task/project/expert/model/shared），两套体系并存。见 CONFLICT_DECISIONS。
ASSET_NAMESPACE: Final[Tuple[str, ...]] = (
    "protected_core", "shared_validated", "domain_private", "project", "task",
)

MEMORY_SCOPE_LEGACY: Final[Tuple[str, ...]] = (
    "task", "project", "expert", "model", "shared",
)

#: 契约 §6 的 `lifecycle_state` 八态。实现侧 `CapabilityEntry.status`
#: 只有 active/experimental/disabled 三值，无语义对应。
ASSET_LIFECYCLE: Final[Tuple[str, ...]] = (
    "draft", "candidate", "validating", "approved", "active", "degraded",
    "deprecated", "retired",
)

ASSET_LIFECYCLE_LEGACY: Final[Tuple[str, ...]] = (
    "active", "experimental", "disabled",
)

# ---------------------------------------------------------------------------
# Goal 契约（契约 §7）—— 整份缺失，由 F-17 起落地
# ---------------------------------------------------------------------------

#: 契约 §7 的 `Goal.status` 十值。⚠️ 冲突：05 卷散文只列 7 态
#: （提出/运行/等待/暂停/阻塞/完成/过期），缺 ready/review/cancelled。
#: 见 CONFLICT_DECISIONS。
GOAL_STATUS_SPEC: Final[Tuple[str, ...]] = (
    "proposed", "ready", "running", "waiting", "paused", "blocked", "review",
    "completed", "cancelled", "expired",
)

#: 契约 §7 的 `goal_type` 五值。
GOAL_TYPE: Final[Tuple[str, ...]] = (
    "explicit", "long_term", "derived", "maintenance", "opportunity",
)

#: 契约 §7 的 Goal 全字段（顺序即建议的存储列序）。
GOAL_FIELDS: Final[Tuple[str, ...]] = (
    "goal_id", "goal_type", "parent_goal_id", "success_criteria",
    "authorization_ref", "budget_ref", "deadline", "no_progress_policy_ref",
    "status", "evidence_refs",
)

# ---------------------------------------------------------------------------
# 统一错误对象（契约 §10）
# ---------------------------------------------------------------------------

#: 契约 §10 要求的七个字段，**当前 0 实现**（由 F-01 落地）。
ERROR_FIELDS: Final[Tuple[str, ...]] = (
    "error_code", "message", "retryable", "correlation_id",
    "related_event_id", "required_action", "safe_to_repeat",
)

#: 契约 §10 的 `safe_to_repeat` 取值域。
#:
#: `safe_to_repeat=unknown` 时**必须先对账或人工确认**，禁止自动重试。
SAFE_TO_REPEAT: Final[Tuple[str, ...]] = ("yes", "no", "unknown")

SAFE_TO_REPEAT_UNKNOWN: Final[str] = "unknown"

#: `required_action` 的标准取值（供 F-01 采用）。
REQUIRED_ACTIONS: Final[Tuple[str, ...]] = (
    "none",                 # 无需动作；客户端不得自动重试
    "retry",                # 可按退避重试
    "reconcile",            # 先对账（副作用未知）
    "human_confirm",        # 需人工确认后继续
    "fix_request",          # 修正请求后重发
)

# ---------------------------------------------------------------------------
# 版本兼容声明（契约 §11）
# ---------------------------------------------------------------------------

#: 契约 §11 要求制品声明的七项兼容信息。
#: ⚠️ 当前只落地 4 项（min_app_version / required_tool_contracts /
#: compatible_requirements / rollback_target），其中 compatible_requirements
#: 是自由字符串列表而非结构化能力要求。
COMPAT_DECLARATION: Final[Tuple[str, ...]] = (
    "platform",
    "min_app_version",
    "tool_contracts",
    "permission_requirements",
    "data_schema",
    "model_capabilities",
    "offline_support",
)

#: 契约 §11：缺失兼容声明的制品不得进入稳定通道。
ALLOW_UNDECLARED_ARTIFACT: Final[bool] = False

# ---------------------------------------------------------------------------
# 文档冲突的代码级裁定（6 条）
# ---------------------------------------------------------------------------

CONFLICT_DECISIONS: Final[Tuple[dict, ...]] = (
    {
        "id": 1,
        "topic": "Goal.status 取值域",
        "spec_side": "09 卷契约 §7：10 值",
        "impl_side": "05 卷散文：7 态",
        "decision": "以 09 卷契约为准（GOAL_STATUS_SPEC）；05 卷散文视为描述性说明",
        "status": "待设计侧确认",
    },
    {
        "id": 2,
        "topic": "privacy_level 枚举",
        "spec_side": "契约 §3：public/internal/sensitive/restricted",
        "impl_side": "core/privacy.py：standard/sensitive/secret/private",
        "decision": (
            "以契约 §3 为准（PRIVACY_LEVEL_SPEC）。legacy 四值仅供迁移对照，"
            "新代码禁止使用；需在客户端与服务端同步切换"
        ),
        "status": "待设计侧确认",
    },
    {
        "id": 3,
        "topic": "sync_status 枚举",
        "spec_side": "契约 §3：pending/in_flight/acknowledged/quarantined",
        "impl_side": "服务端 local/synced；客户端三值；两端都缺 acknowledged",
        "decision": "补齐 acknowledged（SYNC_STATUS_SPEC），回执 confirmed 数组降为辅助字段",
        "status": "待设计侧确认",
    },
    {
        "id": 4,
        "topic": "L 级语义同名不同义",
        "spec_side": "04 卷：L1-L5 故障影响/归因层级",
        "impl_side": "evolution/controller.py：L0-L4 候选变更风险等级",
        "decision": (
            "两者不是同一体系，**必须改名区分**：文档侧为 FAULT_ATTRIBUTION_LEVEL，"
            "实现侧为 CANDIDATE_RISK_LEVEL。不允许共用 'L{n}' 记法"
        ),
        "status": "待设计侧确认",
    },
    {
        "id": 5,
        "topic": "idempotency 枚举",
        "spec_side": "契约 §4：idempotent/keyed/non_idempotent",
        "impl_side": "tools/contract.py：none/key/state-check",
        "decision": "以契约 §4 为准（IDEMPOTENCY_SPEC），建立映射后迁移实现",
        "status": "待设计侧确认",
    },
    {
        "id": 6,
        "topic": "Asset namespace 与记忆 Scope 两套体系",
        "spec_side": "契约 §6：protected_core/shared_validated/domain_private/project/task",
        "impl_side": "memory/store.py::Scope：task/project/expert/model/shared",
        "decision": (
            "两者语义不同（资产域 vs 记忆域），**保留双套但明确命名**："
            "ASSET_NAMESPACE 与 MEMORY_SCOPE。不再让记忆 Scope 冒充资产 namespace"
        ),
        "status": "待设计侧确认",
    },
)

# ---------------------------------------------------------------------------
# E-07 四类信号契约（GAP 第二批第 7 项；04 卷 E-07 仅给 4 个信号名，
# 本节为代码侧单点补写：触发条件 / 输入 / 判据 / 产出形态 / 去重口径）
# ---------------------------------------------------------------------------

EVOLUTION_SIGNAL_FAILURE: Final[str] = "failure"
EVOLUTION_SIGNAL_COST_ANOMALY: Final[str] = "cost_anomaly"
EVOLUTION_SIGNAL_QUALITY_REGRESSION: Final[str] = "quality_regression"
EVOLUTION_SIGNAL_CAPABILITY_GAP: Final[str] = "capability_gap"

EVOLUTION_SIGNAL_TYPES: Final[Tuple[str, ...]] = (
    EVOLUTION_SIGNAL_FAILURE,
    EVOLUTION_SIGNAL_COST_ANOMALY,
    EVOLUTION_SIGNAL_QUALITY_REGRESSION,
    EVOLUTION_SIGNAL_CAPABILITY_GAP,
)

#: target_component 命名规范（统一标识规范）：expert/<id> | tool/<id> |
#: router | budget | release/<version>
EVOLUTION_SIGNAL_TARGET_PATTERN: Final[str] = (
    r"^(expert/[\w\-\.]+|tool/[\w\-\.]+|router|budget|release/[\w\-\.]+)$"
)

#: 产出形态：ProblemTicket（evolution/controller.py），signal 型票据额外
#: 携带 signal_type / target_component / hypothesis / change_scope，由
#: 消费链 run_signal_pipeline() 自动转 propose_candidate。手工票据
#: （无 signal_type）只入观察队列，不自动提案（证据不足保持候选状态，
#: 04 卷 E-07 设计决策）。

#: 判据与去重默认参数（全部可覆盖；判据缺样本时不出信号——证据不足不假设）
EVOLUTION_SIGNAL_SPEC: Final[Dict[str, Dict[str, str]]] = {
    EVOLUTION_SIGNAL_FAILURE: {
        "hook_event": "diagnostic_case_created",
        "trigger": "诊断器对失败任务建案（记录故障分类，非仅现象）",
        "inputs": "DiagnosticCase(case_id/task_id/symptom) + 任务领域",
        "criteria": (
            "建案即出信号；severity 沿用诊断结论（critical/high/…）；"
            "target_component = expert/<任务领域>"
        ),
        "cooldown_seconds": "3600",
        "change_scope": "local",
    },
    EVOLUTION_SIGNAL_COST_ANOMALY: {
        "hook_event": "task_finalized",
        "trigger": "单个真实运行任务成本显著超出近期基线",
        "inputs": "任务账单(result_evidence.budget.usage.cost) + 近期已完成任务成本窗",
        "criteria": (
            "成本 > max(近期中位数×COST_ANOMALY_RATIO, COST_ANOMALY_FLOOR)；"
            "近期样本 < COST_ANOMALY_MIN_RECENT 时证据不足不出信号；"
            "target_component = budget"
        ),
        "cooldown_seconds": "21600",
        "change_scope": "budget_policy",
    },
    EVOLUTION_SIGNAL_QUALITY_REGRESSION: {
        "hook_event": "evolution_monitored",
        "trigger": "发布候选监测质量退化（monitor.assess 已有阈值体系）",
        "inputs": "monitor.assess 结果(decision/reasons) + 候选 target_component",
        "criteria": (
            "reasons 含『成功率较前窗口下降』类质量退化结论即出信号；"
            "target_component = 候选 target_component"
        ),
        "cooldown_seconds": "86400",
        "change_scope": "local",
    },
    EVOLUTION_SIGNAL_CAPABILITY_GAP: {
        "hook_event": "expert_fallback",
        "trigger": "任务意图无专属专家（路由兜底 general）",
        "inputs": "expert_fallback 事件(intent/expert_id)",
        "criteria": (
            "同一 intent 在 CAPABILITY_GAP_WINDOW_SECONDS 内兜底 ≥"
            "CAPABILITY_GAP_MIN_FALLBACKS 次即出信号（不用『连续』口径："
            "命中任务无事件可记，窗口计数可判定）；target_component = expert/<intent>"
        ),
        "cooldown_seconds": "86400",
        "change_scope": "experts",
    },
}

EVOLUTION_SIGNAL_COOLDOWN_SPEC: Final[Dict[str, int]] = {
    t: int(EVOLUTION_SIGNAL_SPEC[t]["cooldown_seconds"])
    for t in EVOLUTION_SIGNAL_TYPES
}

#: 成本异常判据参数（契约默认值，SignalThresholds 可覆盖）
COST_ANOMALY_RATIO: Final[float] = 3.0      # > 近窗中位数 × 3
COST_ANOMALY_FLOOR: Final[float] = 0.05     # 绝对下限（低基数不误报）
COST_ANOMALY_MIN_RECENT: Final[int] = 5     # 近窗样本少于此数 → 证据不足

#: 能力缺口判据参数
CAPABILITY_GAP_MIN_FALLBACKS: Final[int] = 3
CAPABILITY_GAP_WINDOW_SECONDS: Final[int] = 86400

# ---------------------------------------------------------------------------
# E-12 失败案例 → 回归资产（04 卷 E-12，代码侧补写转换规则）
# ---------------------------------------------------------------------------

#: 转换资格：仅已诊断（有故障分类与假设）的诊断案可固化为回归用例
REGRESSION_SOURCE_STATUSES: Final[Tuple[str, ...]] = ("DIAGNOSED",)

#: 证据不足不转换（保持未知，04 卷 E-07/E-12 设计决策：不把假设写成事实）：
#: OPEN=尚未诊断；INCONCLUSIVE=诊断未收敛
REGRESSION_UNKNOWN_STATUSES: Final[Tuple[str, ...]] = ("OPEN", "INCONCLUSIVE")

#: 回归资产落地：运行时数据集（EvolutionController.datasets 内惰性建）
REGRESSION_DATASET_ID: Final[str] = "failures_regression"
REGRESSION_TAG: Final[str] = "regression"

#: 转换规则（一案一案幂等，case_id → reg_<case_id>）：
#:   input_goal = 源任务 user_goal（转换时任务缺失 → 记 unknown 不转换）
#:   expected   = 源任务 acceptance_criteria（回归即重验原验收），
#:                为空时退回 result_nonempty
#:   intent/domain 沿源任务；holdout=False（真实失败不是独立保留集来源）；
#:   tags = [regression, auto, diagnostic_case/<id>, task/<task_id>]

# ---------------------------------------------------------------------------
# E-13 实验编排：队列 / 预算 / 并发 / 停止条件（04 卷 E-13，代码侧补写）
# ---------------------------------------------------------------------------

#: 停止条件取值域（一轮编排的停止原因；预算耗尽/无证据即停止）
EVOLUTION_STOP_BUDGET_EXHAUSTED: Final[str] = "budget_exhausted"
EVOLUTION_STOP_NO_PENDING_EVIDENCE: Final[str] = "no_pending_evidence"
EVOLUTION_STOP_CONCURRENCY_LIMIT: Final[str] = "concurrency_limit_reached"
EVOLUTION_STOP_EVALUATION_ERROR: Final[str] = "evaluation_error"

EVOLUTION_STOP_REASONS: Final[Tuple[str, ...]] = (
    EVOLUTION_STOP_BUDGET_EXHAUSTED,
    EVOLUTION_STOP_NO_PENDING_EVIDENCE,
    EVOLUTION_STOP_CONCURRENCY_LIMIT,
    EVOLUTION_STOP_EVALUATION_ERROR,
)

#: 并发评测上限默认值；「进行中评测」= 候选处于 TESTING/EVALUATING
EVOLUTION_MAX_CONCURRENT_EVALUATIONS: Final[int] = 1