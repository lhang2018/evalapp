/** 客户端更新管理类型（双端协同 §5 / §10.3）。 */

export type UpdateDecision =
  | "update_available"
  | "up_to_date"
  | "not_eligible"
  | "blocked";

export type UpdatePhase =
  | "idle"
  | "available"
  | "verified"
  | "active"
  | "rolled_back"
  | "blocked"
  | "failed";

export interface UpdateCheckEntry {
  name: string;
  ok: boolean;
  detail: string;
}

export interface UpdateTarget {
  release_id: string;
  version: string;
  artifact_digest?: string | null;
  signed_by?: string | null;
  signature_ok: boolean;
  rollback_target?: string | null;
  min_app_version: string;
  channel: string;
  bundle_kind: string;
  delivery_mode: "capability_slot" | "client_binary" | string;
  gray_percent: number;
  created_at?: number;
  eval_report_available?: boolean;
}

export interface ReconcileResponse {
  installation_id: string;
  decision: UpdateDecision;
  update: {
    available: boolean;
    target_release_id?: string | null;
    in_gray_scope: boolean;
    gray_percent: number;
  };
  checks: UpdateCheckEntry[];
  reasons: string[];
  current_release_id: string | null;
  target: UpdateTarget | null;
  server_time: number;
}

export interface UpdateSlot {
  release_id: string;
  version: string;
  digest: string;
  activated_at: number;
}

/** 服务端候选改进阶段（仅评测之后的状态会下发）。 */
export type CandidateSyncStatus =
  | "APPROVAL_PENDING"
  | "RELEASE_READY"
  | "DEPLOYING"
  | "MONITORING"
  | "PROMOTED"
  | "ROLLED_BACK";

export interface CandidateState {
  candidate_id: string;
  status: CandidateSyncStatus | string;
  target_component: string;
  target_domain?: string;
  risk_level?: string;
  publish_suggestion?: string;
  release_id?: string | null;
  created_at?: number | null;
  updated_at: number;
}

export interface CandidateSyncResponse {
  installation_id: string;
  current_release_id: string | null;
  target_release_id?: string | null;
  installation_status?: string;
  candidates: CandidateState[];
  server_time: number;
}

export interface UpdateState {
  version: 1;
  installation_id: string;
  active_slot: "a" | "b";
  slots: { a: UpdateSlot | null; b: UpdateSlot | null };
  /** 最近一次确认健康的版本；回滚唯一可信目标 */
  last_known_good: UpdateSlot | null;
  /** 已下载校验、尚未切换的候选 */
  pending: UpdateSlot | null;
  phase: UpdatePhase;
  target: UpdateTarget | null;
  checks: UpdateCheckEntry[];
  reasons: string[];
  /** 服务端已下发、供透明展示的候选阶段（不代表可激活） */
  candidates: CandidateState[];
  candidates_synced_at: number | null;
  last_checked_at: number | null;
  last_error: string | null;
}

export interface ReconcileRequest {
  installation_id: string;
  device_id?: string;
  platform: string;
  app_version: string;
  current_release_id: string | null;
  tool_contracts: string[];
}

export interface ApplyResultRequest {
  installation_id: string;
  action:
    | "download_started"
    | "verified"
    | "activation_pending"
    | "activated"
    | "activation_failed"
    | "health_check"
    | "rolled_back";
  release_id?: string;
  to_release_id?: string;
  outcome?: string;
  reason?: string;
  apply_id: string;
}

export interface UpdateTransport {
  reconcile(req: ReconcileRequest): Promise<ReconcileResponse>;
  candidateStates(installationId: string): Promise<CandidateSyncResponse>;
  applyResult(
    req: ApplyResultRequest,
  ): Promise<{ ok: boolean; [key: string]: unknown }>;
}
