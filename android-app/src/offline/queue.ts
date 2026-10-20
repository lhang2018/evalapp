/** 本地事件队列：有序、去重、容量受限、确认后删除、连续 checkpoint、死信。 */

import type {
  DeadLetter,
  OfflineEvent,
  OfflineState,
  PrivacyLevel,
  QueueStats,
} from "./types.ts";
import type { OfflineRepository } from "./storage.ts";
import { createEventId } from "./ids.ts";
import { classifyPrivacy, isUploadable } from "./privacy.ts";

export const QUEUE_CAPACITY = 500;

export interface NewEvent {
  event_id?: string;
  event_type: string;
  task_id?: string | null;
  timestamp?: number;
  severity?: OfflineEvent["severity"];
  payload?: Record<string, unknown>;
  privacy_classification?: PrivacyLevel;
}

export function checkpointOf(state: OfflineState): number {
  const pending = state.queue
    .filter((event) => event.uploadable)
    .map((event) => event.seq);
  if (pending.length === 0) return state.next_seq;
  return Math.min(...pending) - 1;
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

  state.next_seq += 1;
  const event: OfflineEvent = {
    event_id: eventId,
    event_type: input.event_type,
    task_id: input.task_id ?? null,
    timestamp: input.timestamp ?? now,
    severity: input.severity ?? "info",
    payload: input.payload ?? {},
    privacy_classification: privacy,
    uploadable: isUploadable(privacy),
    sync_status: "local",
    seq: state.next_seq,
    attempts: 0,
    last_error: null,
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

  /** 待上传事件（仅 standard 且未确认），按 seq 升序。 */
  async unsynced(limit: number = 50): Promise<OfflineEvent[]> {
    const state = await this.repo.load();
    return state.queue
      .filter((e) => e.uploadable && e.sync_status === "local")
      .sort((a, b) => a.seq - b.seq)
      .slice(0, limit);
  }

  ack(eventIds: string[]): Promise<number> {
    const ids = new Set(eventIds);
    return this.repo.update((state) => {
      const before = state.queue.length;
      state.queue = state.queue.filter((e) => !ids.has(e.event_id));
      state.last_acked_seq = checkpointOf(state);
      return before - state.queue.length;
    });
  }

  markFailed(
    eventIds: string[],
    error: string | Record<string, string>,
    maxAttempts: number = 5,
  ): Promise<{ dead: string[]; updated: number }> {
    const ids = new Set(eventIds);
    return this.repo.update((state) => {
      const dead: string[] = [];
      let updated = 0;
      state.queue = state.queue.filter((event) => {
        if (!ids.has(event.event_id)) return true;
        updated += 1;
        event.attempts += 1;
        event.last_error = typeof error === "string"
          ? error
          : (error[event.event_id] ?? "同步失败");
        if (event.attempts >= maxAttempts) {
          const letter: DeadLetter = {
            event,
            reason: event.last_error ?? "超过最大重试次数",
            dead_at: this.now(),
          };
          state.dead_letters.push(letter);
          dead.push(event.event_id);
          return false;
        }
        return true;
      });
      state.last_acked_seq = checkpointOf(state);
      return { dead, updated };
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
      event.sync_status = "local";
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
        (e) => e.uploadable && e.sync_status === "local",
      ).length,
      localOnly: state.queue.filter((e) => !e.uploadable).length,
      dead: state.dead_letters.length,
      checkpoint: checkpointOf(state),
      capacity: QUEUE_CAPACITY,
    }));
  }
}
