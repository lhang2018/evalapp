/** 离线层共享类型（PRD 72-76）。 */

export type PrivacyLevel = "standard" | "sensitive" | "secret" | "private";

export type LocalTaskStatus =
  | "LOCAL_ONLY"
  | "SUBMITTING"
  | "SUBMITTED"
  | "WAITING_DEPENDENCY"
  | "DONE"
  | "NEEDS_RECONCILE";

export interface OfflineEvent {
  event_id: string;
  event_type: string;
  task_id?: string | null;
  timestamp: number;
  severity: "info" | "warn" | "error" | "critical";
  payload: Record<string, unknown>;
  privacy_classification: PrivacyLevel;
  /** 只有 standard 事件允许上传；sensitive 仅本地保留 */
  uploadable: boolean;
  sync_status: "local" | "synced";
  /** 单调递增的客户端序号，用于连续 checkpoint */
  seq: number;
  attempts: number;
  last_error?: string | null;
}

export interface DeadLetter {
  event: OfflineEvent;
  reason: string;
  dead_at: number;
}

export interface LocalTask {
  local_task_id: string;
  /** 幂等键：任务生命周期内保持不变 */
  client_request_id: string;
  server_task_id?: string | null;
  goal: string;
  intent: string;
  risk_level: string;
  status: LocalTaskStatus;
  result?: string | null;
  failure_reason?: string | null;
  created_at: number;
  updated_at: number;
  last_sync_error?: string | null;
}

export interface OfflineState {
  version: 1;
  queue: OfflineEvent[];
  dead_letters: DeadLetter[];
  tasks: LocalTask[];
  last_acked_seq: number;
  next_seq: number;
  last_sync_at: number | null;
}

export interface QueueStats {
  pending: number;
  localOnly: number;
  dead: number;
  checkpoint: number;
  capacity: number;
}
