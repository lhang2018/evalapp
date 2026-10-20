/**
 * 本地事件队列：有序、去重、容量受限、确认后删除、连续 checkpoint。
 * 同步状态机在队列层落地：pending → in_flight → 确认删除 / 回到 pending /
 * quarantined。在传批次带稳定 batch_id，进程重启后由下次 claim 回收。
 */

import type {
  DataLossMarker,
  DeadLetter,
  InFlightBatch,
  OfflineEvent,
  OfflineState,
  PrivacyLevel,
  QueueStats,
  RetentionTier,
  SyncRejection,
} from "./types.ts";
import type { OfflineRepository } from "./storage.ts";
import { createEventId, createId } from "./ids.ts";
import { classifyPrivacy, isUploadable } from "./privacy.ts";
import { classifyRetention, isExpired } from "./retention.ts";

export const QUEUE_CAPACITY = 500;
export const QUEUE_WARNING_RATIO = 0.8;
const MAX_LOSS_MARKERS = 50;
const TIER_RANK: Record<RetentionTier, number> = {
  debug: 0,
  normal: 1,
  critical: 2,
};

export interface NewEvent {
  event_id?: string;
  event_type: string;
  task_id?: string | null;
  run_id?: string | null;
  runtime_manifest_id?: string | null;
  timestamp?: number;
  severity?: OfflineEvent["severity"];
  payload?: Record<string, unknown>;
  privacy_classification?: PrivacyLevel;
  /** 通常由队列自动分配；仅导入/重建场景显式指定 */
  sequence_no?: number;
}

export interface ClaimedBatch {
  batch_id: string;
  events: OfflineEvent[];
  /** 本次 claim 回收的过期在传事件数 */
  recovered: number;
}

export function checkpointOf(state: OfflineState): number {
  const pending = state.queue
    .filter((event) => event.uploadable)
    .map((event) => event.seq);
  if (pending.length === 0) return state.next_seq;
  return Math.min(...pending) - 1;
}

/** 批次部分确认后，保留未结束事件的在传标记，避免并发重复领取。 */
function pruneInFlight(state: OfflineState, batchId: string): void {
  const batch = state.in_flight;
  if (!batch || batch.batch_id !== batchId) return;
  const remaining = batch.event_ids.filter((id) => {
    const event = state.queue.find((item) => item.event_id === id);
    return Boolean(event && event.sync_status === "in_flight");
  });
  state.in_flight = remaining.length > 0
    ? { ...batch, event_ids: remaining }
    : null;
}

/** 清理过期事件并保留可审计的损失标记（补充 C §4）。 */
function recordLoss(
  state: OfflineState,
  reason: DataLossMarker["reason"],
  dropped: OfflineEvent[],
  now: number,
): void {
  if (dropped.length === 0) return;
  const tiers: Record<RetentionTier, number> = {
    critical: 0, normal: 0, debug: 0,
  };
  for (const event of dropped) tiers[event.retention] += 1;
  const seqs = dropped.map((event) => event.seq);
  const marker: DataLossMarker = {
    marker_id: createId("loss"),
    reason,
    dropped: dropped.length,
    event_ids: dropped.map((event) => event.event_id),
    event_types: [...new Set(dropped.map((event) => event.event_type))],
    tiers,
    first_seq: seqs.length ? Math.min(...seqs) : null,
    last_seq: seqs.length ? Math.max(...seqs) : null,
    at: now,
  };
  state.loss_markers.push(marker);
  if (state.loss_markers.length > MAX_LOSS_MARKERS) {
    state.loss_markers = state.loss_markers.slice(-MAX_LOSS_MARKERS);
  }
}

function cleanupExpiredInto(
  state: OfflineState,
  now: number,
): OfflineEvent[] {
  const dropped: OfflineEvent[] = [];
  state.queue = state.queue.filter((event) => {
    if (event.sync_status !== "pending") return true;
    if (event.retention === "critical") return true;
    if (!isExpired(event.retention, event.timestamp, now)) return true;
    dropped.push(event);
    return false;
  });
  recordLoss(state, "expired", dropped, now);
  return dropped;
}

/**
 * 空间不足时按保留等级腾位：只清理等级低于新事件的 pending 事件，
 * debug 先于 normal；critical 永不自动清理。清理结果写入损失标记。
 */
function evictForCapacityInto(
  state: OfflineState,
  now: number,
  incomingRank: number,
): OfflineEvent[] {
  const need = state.queue.length - QUEUE_CAPACITY + 1;
  if (need <= 0) return [];
  const candidates = state.queue
    .filter((event) => event.sync_status === "pending" &&
      TIER_RANK[event.retention] < incomingRank)
    .sort((a, b) =>
      TIER_RANK[a.retention] - TIER_RANK[b.retention] || a.seq - b.seq);
  const dropped = candidates.slice(0, need);
  if (dropped.length === 0) return [];
  const ids = new Set(dropped.map((event) => event.event_id));
  state.queue = state.queue.filter((event) => !ids.has(event.event_id));
  recordLoss(state, "capacity", dropped, now);
  return dropped;
}

function nextSequence(
  state: OfflineState,
  runKey: string,
  requested?: number,
): number {
  if (requested !== undefined) {
    if (!Number.isInteger(requested) || requested <= 0) {
      throw new Error("sequence_no 必须是正整数");
    }
    state.next_sequence[runKey] = Math.max(
      state.next_sequence[runKey] ?? 0,
      requested,
    );
    return requested;
  }
  const value = (state.next_sequence[runKey] ?? 0) + 1;
  state.next_sequence[runKey] = value;
  return value;
}

export function enqueueInto(
  state: OfflineState,
  input: NewEvent,
  now: number = Date.now(),
): OfflineEvent {
  const eventId = input.event_id ?? createEventId();
  const existing = state.queue.find((e) => e.event_id === eventId) ??
    state.dead_letters.find((d) => d.event.event_id === eventId)?.event;
  if (existing) return existing;

  const privacy =
    input.privacy_classification ??
    classifyPrivacy(input.event_type, input.payload ?? {});
  if (privacy === "secret" || privacy === "private") {
    throw new Error("事件包含敏感信息，禁止入队");
  }

  const severity = input.severity ?? "info";
  const retention = classifyRetention(input.event_type, severity);
  cleanupExpiredInto(state, now);
  if (state.queue.length >= QUEUE_CAPACITY) {
    evictForCapacityInto(state, now, TIER_RANK[retention]);
  }
  if (state.queue.length >= QUEUE_CAPACITY) {
    throw new Error("本地事件队列已满，请先同步或清理死信");
  }

  const runId = input.run_id ?? input.task_id ?? null;
  const runKey = runId ?? "client";
  state.next_seq += 1;
  const event: OfflineEvent = {
    event_id: eventId,
    event_type: input.event_type,
    task_id: input.task_id ?? null,
    run_id: runId,
    runtime_manifest_id: input.runtime_manifest_id ?? null,
    timestamp: input.timestamp ?? now,
    clock_source: "device",
    severity,
    payload: input.payload ?? {},
    privacy_classification: privacy,
    uploadable: isUploadable(privacy),
    retention,
    sync_status: "pending",
    sequence_no: nextSequence(state, runKey, input.sequence_no),
    seq: state.next_seq,
    attempts: 0,
    last_error: null,
    batch_id: null,
    in_flight_at: null,
    quarantined_at: null,
  };
  state.queue.push(event);
  return event;
}

export class EventQueue {
  private readonly repo: OfflineRepository;
  private readonly now: () => number;

  constructor(repo: OfflineRepository, now: () => number = () => Date.now()) {
    this.repo = repo;
    this.now = now;
  }

  enqueue(input: NewEvent): Promise<OfflineEvent> {
    return this.repo.update((state) => enqueueInto(state, input, this.now()));
  }

  async list(): Promise<OfflineEvent[]> {
    const state = await this.repo.load();
    return [...state.queue].sort((a, b) => a.seq - b.seq);
  }

  /** 待上传事件（仅 standard 且 pending），按本地 seq 升序。 */
  async unsynced(limit: number = 50): Promise<OfflineEvent[]> {
    const state = await this.repo.load();
    return state.queue
      .filter((e) => e.uploadable && e.sync_status === "pending")
      .sort((a, b) => a.seq - b.seq)
      .slice(0, limit);
  }

  /**
   * 领取一个上传批次。已有在传批次且未过期时返回 null（避免并发重复上传）；
   * 过期（进程被杀/网络挂起）则回收为 pending 后重新领取。event_id 不变，
   * 服务端幂等去重保证重复上传安全。
   */
  claimBatch(
    limit: number = 50,
    staleMs: number = 0,
  ): Promise<ClaimedBatch | null> {
    const now = this.now();
    return this.repo.update((state) => {
      let recovered = 0;
      const current = state.in_flight;
      if (current) {
        const claimedAt = Number.isFinite(current.claimed_at)
          ? current.claimed_at
          : 0;
        if (now - claimedAt < staleMs) return null;
        const ids = new Set(current.event_ids);
        for (const event of state.queue) {
          if (ids.has(event.event_id) && event.sync_status === "in_flight") {
            event.sync_status = "pending";
            event.batch_id = null;
            event.in_flight_at = null;
            recovered += 1;
          }
        }
        state.in_flight = null;
      }

      const pending = state.queue
        .filter((e) => e.uploadable && e.sync_status === "pending")
        .sort((a, b) => a.seq - b.seq)
        .slice(0, Math.max(1, limit));
      if (pending.length === 0) {
        if (recovered > 0) {
          state.last_acked_seq = checkpointOf(state);
          return { batch_id: "", events: [], recovered };
        }
        return null;
      }

      const batchId = createId("batch");
      for (const event of pending) {
        event.sync_status = "in_flight";
        event.batch_id = batchId;
        event.in_flight_at = now;
      }
      const batch: InFlightBatch = {
        batch_id: batchId,
        event_ids: pending.map((e) => e.event_id),
        claimed_at: now,
        attempts: 1,
      };
      state.in_flight = batch;
      return { batch_id: batchId, events: pending, recovered };
    });
  }

  /** 传输失败：在传事件回到 pending；超过尝试上限进入死信（仍可人工重试）。 */
  releaseBatch(
    batchId: string,
    error: string,
    maxAttempts: number = 5,
  ): Promise<{ dead: string[]; released: number }> {
    return this.repo.update((state) => {
      if (!state.in_flight || state.in_flight.batch_id !== batchId) {
        return { dead: [], released: 0 };
      }
      const ids = new Set(state.in_flight.event_ids);
      const dead: string[] = [];
      let released = 0;
      state.queue = state.queue.filter((event) => {
        if (!ids.has(event.event_id) || event.sync_status !== "in_flight") {
          return true;
        }
        event.attempts += 1;
        event.last_error = error;
        event.batch_id = null;
        event.in_flight_at = null;
        if (event.attempts >= maxAttempts) {
          const letter: DeadLetter = {
            event,
            reason: error || "超过最大重试次数",
            dead_at: this.now(),
          };
          state.dead_letters.push(letter);
          dead.push(event.event_id);
          return false;
        }
        event.sync_status = "pending";
        released += 1;
        return true;
      });
      state.in_flight = null;
      state.last_acked_seq = checkpointOf(state);
      return { dead, released };
    });
  }

  /** 服务端回执确认：删除已确认事件，推进连续 checkpoint。 */
  acknowledgeBatch(batchId: string, eventIds: string[]): Promise<number> {
    const ids = new Set(eventIds);
    return this.repo.update((state) => {
      const before = state.queue.length;
      state.queue = state.queue.filter((event) => !ids.has(event.event_id));
      const removed = before - state.queue.length;
      pruneInFlight(state, batchId);
      state.last_acked_seq = checkpointOf(state);
      return removed;
    });
  }

  /** 永久校验错误：事件保留在本地等待人工审查，不无限重试。 */
  quarantineBatch(
    batchId: string,
    items: SyncRejection[],
  ): Promise<number> {
    const errors = new Map(items.map((item) => [item.event_id, item.error]));
    const now = this.now();
    return this.repo.update((state) => {
      let count = 0;
      for (const event of state.queue) {
        if (!errors.has(event.event_id)) continue;
        event.sync_status = "quarantined";
        event.last_error = errors.get(event.event_id) || "服务端拒绝";
        event.quarantined_at = now;
        event.batch_id = null;
        event.in_flight_at = null;
        count += 1;
      }
      pruneInFlight(state, batchId);
      state.last_acked_seq = checkpointOf(state);
      return count;
    });
  }

  async quarantined(): Promise<OfflineEvent[]> {
    const state = await this.repo.load();
    return state.queue
      .filter((event) => event.sync_status === "quarantined")
      .sort((a, b) => a.seq - b.seq);
  }

  retryQuarantined(eventId: string): Promise<boolean> {
    return this.repo.update((state) => {
      const event = state.queue.find((e) => e.event_id === eventId);
      if (!event || event.sync_status !== "quarantined") return false;
      event.sync_status = "pending";
      event.attempts = 0;
      event.last_error = null;
      event.quarantined_at = null;
      state.last_acked_seq = checkpointOf(state);
      return true;
    });
  }

  deadLetters(): Promise<DeadLetter[]> {
    return this.repo.load().then((state) => [...state.dead_letters]);
  }

  retryDeadLetter(eventId: string): Promise<boolean> {
    return this.repo.update((state) => {
      const index = state.dead_letters.findIndex(
        (d) => d.event.event_id === eventId,
      );
      if (index < 0) return false;
      cleanupExpiredInto(state, this.now());
      if (state.queue.length >= QUEUE_CAPACITY) {
        throw new Error("本地事件队列已满，请先同步");
      }
      const [letter] = state.dead_letters.splice(index, 1);
      const event = letter.event;
      event.attempts = 0;
      event.last_error = null;
      event.sync_status = "pending";
      event.batch_id = null;
      event.in_flight_at = null;
      state.queue.push(event);
      state.last_acked_seq = checkpointOf(state);
      return true;
    });
  }

  clearDeadLetters(): Promise<number> {
    return this.repo.update((state) => {
      const count = state.dead_letters.length;
      state.dead_letters = [];
      return count;
    });
  }

  checkpoint(): Promise<number> {
    return this.repo.load().then(checkpointOf);
  }

  markSynced(at: number): Promise<void> {
    return this.repo.update((state) => {
      state.last_sync_at = at;
    });
  }

  /** 主动清理过期事件；容量压力下由 enqueue 自动触发。 */
  cleanup(): Promise<{ dropped: number; markers: number }> {
    return this.repo.update((state) => {
      const before = state.loss_markers.length;
      const dropped = cleanupExpiredInto(state, this.now());
      if (dropped.length) state.last_acked_seq = checkpointOf(state);
      return {
        dropped: dropped.length,
        markers: state.loss_markers.length - before,
      };
    });
  }

  lossMarkers(): Promise<DataLossMarker[]> {
    return this.repo.load().then((state) => [...state.loss_markers]);
  }

  stats(): Promise<QueueStats> {
    return this.repo.load().then((state) => {
      const usage = state.queue.length;
      const usageRatio = usage / QUEUE_CAPACITY;
      return {
        pending: state.queue.filter(
          (e) => e.uploadable && e.sync_status === "pending",
        ).length,
        inFlight: state.queue.filter(
          (e) => e.uploadable && e.sync_status === "in_flight",
        ).length,
        quarantined: state.queue.filter(
          (e) => e.uploadable && e.sync_status === "quarantined",
        ).length,
        localOnly: state.queue.filter((e) => !e.uploadable).length,
        dead: state.dead_letters.length,
        checkpoint: checkpointOf(state),
        capacity: QUEUE_CAPACITY,
        usage,
        usageRatio,
        warning: usageRatio >= QUEUE_WARNING_RATIO,
        lossMarkers: state.loss_markers.length,
      };
    });
  }
}
