/** 离线层共享类型（PRD 72-76）。 */

export type PrivacyLevel = "standard" | "sensitive" | "secret" | "private";

export type LocalTaskStatus =
  | "LOCAL_ONLY"
  | "SUBMITTING"
  | "SUBMITTED"
  | "WAITING_DEPENDENCY"
  | "DONE"
  | "NEEDS_RECONCILE";

/**
 * 事件同步状态机：pending → in_flight →
 *   ack（删除）| 网络失败回到 pending | 永久校验错误进入 quarantined。
 * 隔离事件保留在本地队列，等待人工重试，不会被静默删除。
 */
export type EventSyncStatus = "pending" | "in_flight" | "quarantined";

/** 事件时间来自设备时钟；可能存在偏差，因果顺序以 sequence_no 为准。 */
export type ClockSource = "device";

export interface OfflineEvent {
  event_id: string;
  event_type: string;
  task_id?: string | null;
  /** 同一 run 的稳定标识；无 run 时由 task_id 或 client 代替 */
  run_id: string | null;
  /** 产生事件时的不可变运行版本清单引用 */
  runtime_manifest_id: string | null;
  /** 设备时钟下的创建时间（毫秒）；同步时转为秒 */
  timestamp: number;
  clock_source: ClockSource;
  severity: "info" | "warn" | "error" | "critical";
  payload: Record<string, unknown>;
  privacy_classification: PrivacyLevel;
  /** 只有 standard 事件允许上传；sensitive 仅本地保留 */
  uploadable: boolean;
  sync_status: EventSyncStatus;
  /** 同一 run 内单调递增，用于还原因果顺序 */
  sequence_no: number;
  /** 客户端队列全局序号，用于本地 FIFO 与连续 checkpoint */
  seq: number;
  attempts: number;
  last_error?: string | null;
  /** 当前在传批次的稳定 ID；重试不重新生成 event_id */
  batch_id?: string | null;
  in_flight_at?: number | null;
  quarantined_at?: number | null;
}

export interface SyncRejection {
  event_id: string;
  error: string;
}

export interface InFlightBatch {
  batch_id: string;
  event_ids: string[];
  claimed_at: number;
  attempts: number;
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
  /** 每个 run 已分配的最大 sequence_no */
  next_sequence: Record<string, number>;
  /** 进程被杀后遗留的在传批次，由下次 claim 回收 */
  in_flight: InFlightBatch | null;
}

export interface QueueStats {
  pending: number;
  inFlight: number;
  quarantined: number;
  localOnly: number;
  dead: number;
  checkpoint: number;
  capacity: number;
}
