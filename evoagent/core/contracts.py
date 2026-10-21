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

from typing import Final, Tuple

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