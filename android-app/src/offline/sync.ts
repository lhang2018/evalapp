/** 事件同步引擎：批量上传、只 ack 服务端确认的事件、指数退避与死信。 */

import type { OfflineEvent } from "./types.ts";
import type { EventQueue } from "./queue.ts";
import type { LocalTaskService, TaskFlushResult } from "./tasks.ts";

export interface SyncRejection {
  event_id: string;
  error: string;
}

export interface SyncResponse {
  accepted: string[];
  duplicates: string[];
  rejected: SyncRejection[];
}

export interface EventSyncTransport {
  syncEvents(events: OfflineEvent[]): Promise<SyncResponse>;
}

export interface SyncOptions {
  batchSize?: number;
  baseDelayMs?: number;
  maxDelayMs?: number;
  maxAttempts?: number;
  now?: () => number;
  jitter?: () => number;
}

export interface SyncResult {
  ok: boolean;
  skipped: boolean;
  accepted: number;
  duplicates: number;
  rejected: number;
  dead: number;
  error?: string;
  tasks?: TaskFlushResult;
}

export class SyncEngine {
  private failures = 0;
  private nextAttemptAt = 0;
  private running: Promise<SyncResult> | null = null;

  private readonly queue: EventQueue;
  private readonly transport: EventSyncTransport;
  private readonly tasks: LocalTaskService | null;
  private readonly batchSize: number;
  private readonly baseDelayMs: number;
  private readonly maxDelayMs: number;
  private readonly maxAttempts: number;
  private readonly now: () => number;
  private readonly jitter: () => number;

  constructor(
    queue: EventQueue,
    transport: EventSyncTransport,
    tasks: LocalTaskService | null = null,
    options: SyncOptions = {},
  ) {
    this.queue = queue;
    this.transport = transport;
    this.tasks = tasks;
    this.batchSize = options.batchSize ?? 50;
    this.baseDelayMs = options.baseDelayMs ?? 2000;
    this.maxDelayMs = options.maxDelayMs ?? 300000;
    this.maxAttempts = options.maxAttempts ?? 5;
    this.now = options.now ?? (() => Date.now());
    this.jitter = options.jitter ?? (() => 0.8 + Math.random() * 0.4);
  }

  get nextRetryAt(): number {
    return this.nextAttemptAt;
  }

  flush(force: boolean = false): Promise<SyncResult> {
    if (this.running) return this.running;
    this.running = this._flush(force).finally(() => {
      this.running = null;
    });
    return this.running;
  }

  private async _flush(force: boolean): Promise<SyncResult> {
    const result: SyncResult = {
      ok: true,
      skipped: false,
      accepted: 0,
      duplicates: 0,
      rejected: 0,
      dead: 0,
    };

    if (!force && this.now() < this.nextAttemptAt) {
      result.skipped = true;
      return result;
    }

    while (true) {
      const batch = await this.queue.unsynced(this.batchSize);
      if (batch.length === 0) break;

      let response: SyncResponse;
      try {
        response = await this.transport.syncEvents(batch);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        const { dead } = await this.queue.markFailed(
          batch.map((e) => e.event_id),
          message,
          this.maxAttempts,
        );
        this.failures += 1;
        this.nextAttemptAt = this.now() + this.backoffDelay();
        result.ok = false;
        result.error = message;
        result.dead += dead.length;
        return result;
      }

      const accepted = response.accepted ?? [];
      const duplicates = response.duplicates ?? [];
      const rejected = response.rejected ?? [];

      await this.queue.ack([...accepted, ...duplicates]);
      result.accepted += accepted.length;
      result.duplicates += duplicates.length;

      if (rejected.length > 0) {
        const errors: Record<string, string> = {};
        for (const item of rejected) {
          errors[item.event_id] = item.error || "服务端拒绝";
        }
        const { dead } = await this.queue.markFailed(
          Object.keys(errors),
          errors,
          this.maxAttempts,
        );
        result.rejected += rejected.length;
        result.dead += dead.length;
        // 被拒事件留到下一轮再试，避免同一轮内被立即重复计数
        break;
      }

      if (accepted.length === 0 && duplicates.length === 0) {
        break; // 防御：服务端无进展时终止循环
      }
    }

    this.failures = 0;
    this.nextAttemptAt = 0;
    await this.queue.markSynced(this.now());

    if (this.tasks) {
      result.tasks = await this.tasks.flushPending();
    }
    return result;
  }

  private backoffDelay(): number {
    const exponent = Math.max(0, this.failures - 1);
    const raw = this.baseDelayMs * 2 ** exponent;
    return Math.floor(Math.min(this.maxDelayMs, raw) * this.jitter());
  }
}
