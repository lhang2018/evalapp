/**
 * 事件同步引擎：领取批次 → 上传 → 按回执确认；网络错误退避重试，
 * 永久校验错误进入隔离队列。只 ack 服务端明确确认的事件。
 */

import type { OfflineEvent, SyncRejection } from "./types.ts";
import type { EventQueue } from "./queue.ts";
import type { LocalTaskService, TaskFlushResult } from "./tasks.ts";

export type { SyncRejection } from "./types.ts";

export interface SyncReceipt {
  receipt_id: string;
  batch_id: string | null;
  replayed: boolean;
  received_at: number;
  protocol_version?: string;
  confirmed_count?: number;
}

export interface SyncResponse {
  accepted: string[];
  duplicates: string[];
  rejected: SyncRejection[];
  /** 服务端回执协议 1.0：显式已确认/未确认事件 */
  confirmed?: string[];
  unconfirmed?: string[];
  protocol_version?: string;
  receipt?: SyncReceipt;
}

export interface EventSyncTransport {
  syncEvents(events: OfflineEvent[], batchId?: string): Promise<SyncResponse>;
}

export interface SyncOptions {
  batchSize?: number;
  baseDelayMs?: number;
  maxDelayMs?: number;
  maxAttempts?: number;
  /** 在传批次超过该时长视为进程/网络中断，可被回收 */
  staleMs?: number;
  now?: () => number;
  jitter?: () => number;
}

export interface SyncResult {
  ok: boolean;
  skipped: boolean;
  accepted: number;
  duplicates: number;
  rejected: number;
  quarantined: number;
  recovered: number;
  dead: number;
  error?: string;
  tasks?: TaskFlushResult;
}

/** 服务端 400 携带逐事件拒绝明细；其余错误按可重试传输失败处理。 */
function rejectedFromError(error: unknown): SyncRejection[] | null {
  if (!error || typeof error !== "object") return null;
  const status = (error as { status?: unknown }).status;
  const body = (error as { body?: unknown }).body;
  if (status !== 400 || !body || typeof body !== "object") return null;
  const raw = (body as { rejected?: unknown }).rejected;
  if (!Array.isArray(raw)) return null;
  const items: SyncRejection[] = [];
  for (const entry of raw) {
    if (!entry || typeof entry !== "object") continue;
    const eventId = (entry as { event_id?: unknown }).event_id;
    const message = (entry as { error?: unknown }).error;
    if (typeof eventId !== "string" || !eventId) continue;
    items.push({
      event_id: eventId,
      error: typeof message === "string" ? message : "服务端拒绝",
    });
  }
  return items.length > 0 ? items : null;
}

export class SyncEngine {
  private failures = 0;
  private nextAttemptAt = 0;
  private running: Promise<SyncResult> | null = null;
  private sessionStarted = false;

  private readonly queue: EventQueue;
  private readonly transport: EventSyncTransport;
  private readonly tasks: LocalTaskService | null;
  private readonly batchSize: number;
  private readonly baseDelayMs: number;
  private readonly maxDelayMs: number;
  private readonly maxAttempts: number;
  private readonly staleMs: number;
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
    this.staleMs = options.staleMs ?? 300000;
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
      quarantined: 0,
      recovered: 0,
      dead: 0,
    };

    if (!force && this.now() < this.nextAttemptAt) {
      result.skipped = true;
      return result;
    }

    // 新会话首次同步：把上次进程遗留的在传批次立即回收（event_id 幂等）
    const staleMs = this.sessionStarted ? this.staleMs : 0;
    this.sessionStarted = true;

    while (true) {
      const claimed = await this.queue.claimBatch(this.batchSize, staleMs);
      if (!claimed) break;
      result.recovered += claimed.recovered;
      if (claimed.events.length === 0) break;

      let response: SyncResponse;
      try {
        response = await this.transport.syncEvents(
          claimed.events,
          claimed.batch_id,
        );
      } catch (error) {
        const permanent = rejectedFromError(error);
        if (permanent) {
          await this.queue.quarantineBatch(claimed.batch_id, permanent);
          result.rejected += permanent.length;
          result.quarantined += permanent.length;
          // 隔离后继续处理下一批；被隔离事件不再参与待上传集合
          continue;
        }

        const message = error instanceof Error ? error.message : String(error);
        const { dead } = await this.queue.releaseBatch(
          claimed.batch_id,
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
      // 协议 1.0 的 confirmed 是权威列表；旧服务端回退为 accepted+duplicates
      const confirmed = response.confirmed ?? [...accepted, ...duplicates];

      await this.queue.acknowledgeBatch(claimed.batch_id, confirmed);
      result.accepted += accepted.length;
      result.duplicates += duplicates.length;

      if (rejected.length > 0) {
        const count = await this.queue.quarantineBatch(
          claimed.batch_id,
          rejected,
        );
        result.rejected += rejected.length;
        result.quarantined += count;
      }

      if (confirmed.length === 0 && rejected.length === 0) {
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
