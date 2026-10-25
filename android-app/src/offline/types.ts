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

/**
 * 保留等级：critical 永不自动清理；normal 保留 7 天；debug 保留 1 天。
 * 空间紧张时先清理 debug，再清理最旧的 normal，并记录数据损失标记。
 */
export type RetentionTier = "critical" | "normal" | "debug";

export interface DataLossMarker {
  marker_id: string;
  reason: "expired" | "capacity";
  dropped: number;
  event_ids: string[];
  event_types: string[];
  tiers: Record<RetentionTier, number>;
  first_seq: number | null;
  last_seq: number | null;
  at: number;
}

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
  /** 容量治理保留等级（补充 C §4） */
  retention: RetentionTier;
  /** 诊断附件引用（补充 C §6）；附件可稍后补传 */
  attachment_refs: string[];
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

/** 服务端批次回执的本地副本（补充 C §3.2：sync_receipts 独立持久化）。 */
export interface LocalSyncReceipt {
  receipt_id: string;
  batch_id: string | null;
  received_at: number;
  recorded_at: number;
  accepted: number;
  duplicates: number;
  rejected: number;
  confirmed: number;
  replayed: boolean;
  protocol_version: string | null;
}

export type AttachmentStatus = "pending" | "uploading" | "complete" | "failed";

/** 诊断附件的本地上传任务（补充 C §6）：只持久化元数据与分块进度。 */
export interface LocalAttachment {
  attachment_id: string;
  file_name: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  chunk_size: number;
  chunk_count: number;
  /** 服务端已确认接收的分块序号 */
  uploaded_chunks: number[];
  status: AttachmentStatus;
  /** 高敏感材料必须显式授权后才允许上传 */
  consent: boolean;
  created_at: number;
  updated_at: number;
  completed_at: number | null;
  last_error: string | null;
}

export interface UserInputRequest {
  request_id: string;
  question: string;
  phase: string;
  answered: boolean;
  created_at: number;
}

/** 统一任务契约输入（ALN-23-A）：三入口（REST/CLI/客户端）字段同源。 */
export interface TaskContractInput {
  constraints?: string[];
  acceptance_criteria?: string[];
  target_object?: string;
  expected_output?: string;
  required_tools?: string[];
  required_permissions?: string[];
  open_questions?: string[];
  complexity?: string;
  requires_network?: boolean;
  workflow_suitable?: boolean;
}

export interface LocalTask {
  server_status?: string;
  current_run_id?: string | null;
  waiting_request?: UserInputRequest | null;
  waiting_approvals?: string[];
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
  /** 统一任务契约（ALN-23-A）：随任务持久化并在提交时透传服务端 */
  contract?: TaskContractInput | null;
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
  /** 因过期或容量压力被清理的事件记录；不静默丢数据 */
  loss_markers: DataLossMarker[];
  /** 最近服务端批次回执（上限 20 条，仅本地证据） */
  sync_receipts: LocalSyncReceipt[];
  /** 连续传输失败次数；成功同步后清零 */
  sync_failures: number;
  last_failure_at: number | null;
  /** 诊断附件上传任务（补充 C §6） */
  attachments: LocalAttachment[];
}

export interface QueueStats {
  pending: number;
  inFlight: number;
  quarantined: number;
  localOnly: number;
  dead: number;
  checkpoint: number;
  capacity: number;
  usage: number;
  usageRatio: number;
  warning: boolean;
  lossMarkers: number;
  receipts: number;
  lastReceiptAt: number | null;
  consecutiveFailures: number;
  lastSuccessAt: number | null;
  /** 服务端长时间不可用：连续失败或距上次成功过久且仍有待同步事件 */
  serverUnavailable: boolean;
}
