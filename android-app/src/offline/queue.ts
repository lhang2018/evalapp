/**
 * 本地事件队列：有序、去重、容量受限、确认后删除、连续 checkpoint。
 * 同步状态机在队列层落地：pending → in_flight → 确认删除 / 回到 pending /
 * quarantined。在传批次带稳定 batch_id，进程重启后由下次 claim 回收。
 */

import type {
  DeadLetter,
  InFlightBatch,
  OfflineEvent,
  OfflineState,
  PrivacyLevel,
  QueueStats,
  SyncRejection,
} from "./types.ts";
import type { OfflineRepository } from "./storage.ts";
import { createEventId, createId } from "./ids.ts";
import { classifyPrivacy, isUploadable } from "./privacy.ts";

export const QUEUE_CAPACITY = 500;

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
    severity: input.severity ?? "info",
    payload: input.payload ?? {},
    privacy_classification: privacy,
    uploadable: isUploadable(privacy),
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

  stats(): Promise<QueueStats> {
    return this.repo.load().then((state) => ({
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
    }));
  }
}
