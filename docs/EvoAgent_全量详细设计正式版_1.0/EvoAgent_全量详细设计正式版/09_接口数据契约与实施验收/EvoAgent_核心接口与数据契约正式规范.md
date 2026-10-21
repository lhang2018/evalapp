# EvoAgent 核心接口与数据契约正式规范

## 1. 统一标识
- `task_id`：用户目标的一次逻辑任务，创建后稳定。
- `run_id`：一次具体执行过程或恢复尝试，关联父 run。
- `node_run_id`：一次 DAG 节点执行。
- `goal_id`：长期目标或维护目标。
- `event_id`：全局唯一事件 ID，重试上传时不变。
- `batch_id`：同步批次 ID。
- `runtime_manifest_id`：不可变运行组件组合。
- `asset_id + version`：能力资产稳定身份与不可变版本。
- `approval_id`：绑定具体动作摘要的授权记录。
- `release_id`：正式发布清单 ID。

## 2. Task 契约
```json
{
  "task_id": "uuid",
  "goal_id": "uuid-or-null",
  "origin": "user|scheduled|derived|opportunity",
  "objective": "string",
  "constraints": {},
  "acceptance_contract_ref": "id/version",
  "risk_class": "low|medium|high|critical",
  "authorization_ref": "policy-id/version",
  "budget_ref": "budget-id",
  "status": "created|classified|planned|running|waiting_approval|recovering|verifying|succeeded|failed|cancelled|needs_user",
  "runtime_manifest_id": "immutable-id",
  "idempotency_key": "string",
  "created_at": "RFC3339",
  "updated_at": "RFC3339"
}
```

## 3. Runtime Event 契约
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

## 4. Tool Contract
```json
{
  "tool_id": "stable-name",
  "version": "immutable-version",
  "input_schema": {},
  "output_schema": {},
  "risk_class": "low|medium|high|critical",
  "required_permissions": [],
  "timeout_seconds": 30,
  "idempotency": "idempotent|keyed|non_idempotent",
  "offline_capable": false,
  "requires_network": true,
  "cost_model": {},
  "health_check_ref": "optional",
  "contract_test_ref": "required"
}
```

## 5. Runtime Manifest 契约
```json
{
  "runtime_manifest_id": "immutable-id",
  "created_at": "RFC3339",
  "app_version": "version",
  "core_version": "version",
  "runtime_adapter": {"name": "mock|dsh|native", "version": "pinned"},
  "model_profile_ref": "id/version",
  "router_policy_ref": "id/version",
  "context_policy_ref": "id/version",
  "expert_ref": "id/version",
  "workflow_ref": "id/version-or-null",
  "tool_versions": [],
  "evaluation_suite_ref": "id/version",
  "environment_digest": "sha256"
}
```

## 6. Capability Asset 契约
```json
{
  "asset_id": "stable-id",
  "asset_type": "knowledge|skill|tool|workflow|expert|capability_pack|model_profile|evaluation_suite",
  "namespace": "protected_core|shared_validated|domain_private|project|task",
  "version": "immutable-version",
  "content_digest": "sha256",
  "source_refs": [],
  "dependency_refs": [],
  "compatibility": {},
  "lifecycle_state": "draft|candidate|validating|approved|active|degraded|deprecated|retired",
  "evaluation_evidence_refs": [],
  "rollback_ref": "optional"
}
```

## 7. Goal 契约
```json
{
  "goal_id": "uuid",
  "goal_type": "explicit|long_term|derived|maintenance|opportunity",
  "parent_goal_id": "optional",
  "success_criteria": [],
  "authorization_ref": "policy-id/version",
  "budget_ref": "budget-id",
  "deadline": "optional",
  "no_progress_policy_ref": "policy-id/version",
  "status": "proposed|ready|running|waiting|paused|blocked|review|completed|cancelled|expired",
  "evidence_refs": []
}
```

## 8. Side-effect Ledger 契约
```json
{
  "effect_id": "uuid",
  "task_id": "uuid",
  "run_id": "uuid",
  "tool_id": "stable-name",
  "idempotency_key": "stable-key",
  "effect_summary": "string",
  "status": "planned|submitted|confirmed|failed|unknown|compensated",
  "external_receipt_ref": "optional",
  "last_reconciled_at": "RFC3339-or-null"
}
```

## 9. 同步回执
服务端回执必须包括 `batch_id`、已持久化的 event IDs 或可验证连续范围、未确认/拒绝事件及原因、服务端持久化时间和协议版本。HTTP 成功或连接成功本身不等于同步成功。

## 10. 错误返回
统一错误对象至少包含 `error_code`、`message`、`retryable`、`correlation_id`、`related_event_id`、`required_action` 和 `safe_to_repeat`。`safe_to_repeat=unknown` 时必须先对账或人工确认。

## 11. 版本兼容
能力发布必须声明最低 App 版本、工具契约、权限要求、数据 Schema、模型能力要求、离线支持和回滚目标。缺失兼容声明的制品不得进入稳定通道。
