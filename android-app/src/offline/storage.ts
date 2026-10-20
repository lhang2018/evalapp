/** 可注入的键值存储与整文档快照（RN 侧用 AsyncStorage 适配）。 */

import type {
  DataLossMarker,
  DeadLetter,
  EventSyncStatus,
  InFlightBatch,
  LocalSyncReceipt,
  OfflineEvent,
  OfflineState,
  PrivacyLevel,
  RetentionTier,
} from "./types.ts";
import { classifyRetention } from "./retention.ts";

export interface KeyValueStore {
  getItem(key: string): Promise<string | null>;
  setItem(key: string, value: string): Promise<void>;
  removeItem(key: string): Promise<void>;
}

export class MemoryStore implements KeyValueStore {
  private data = new Map<string, string>();

  async getItem(key: string): Promise<string | null> {
    return this.data.has(key) ? (this.data.get(key) as string) : null;
  }

  async setItem(key: string, value: string): Promise<void> {
    this.data.set(key, value);
  }

  async removeItem(key: string): Promise<void> {
    this.data.delete(key);
  }
}

export const STATE_KEY = "offline_state_v1";

export function emptyState(): OfflineState {
  return {
    version: 1,
    queue: [],
    dead_letters: [],
    tasks: [],
    last_acked_seq: 0,
    next_seq: 0,
    last_sync_at: null,
    next_sequence: {},
    in_flight: null,
    loss_markers: [],
    sync_receipts: [],
    sync_failures: 0,
    last_failure_at: null,
  };
}

const PRIVACY_LEVELS: PrivacyLevel[] = [
  "standard", "sensitive", "secret", "private",
];
const SYNC_STATES: EventSyncStatus[] = ["pending", "in_flight", "quarantined"];

function asNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value)
    ? value
    : fallback;
}

function runKeyOf(taskId: unknown, runId: unknown): string | null {
  if (typeof runId === "string" && runId) return runId;
  if (typeof taskId === "string" && taskId) return taskId;
  return null;
}

/** 兼容旧快照：sync_status 由 local/synced 归一为状态机取值，并补序号。 */
function normalizeEvent(
  raw: unknown,
  sequence: Record<string, number>,
  nextSeq: () => number,
): OfflineEvent | null {
  if (!raw || typeof raw !== "object") return null;
  const event = raw as OfflineEvent;
  if (typeof event.event_id !== "string" || !event.event_id) return null;

  const runId = runKeyOf(event.task_id, event.run_id);
  const key = runId ?? "client";
  const legacy = String(event.sync_status ?? "pending");
  const syncStatus: EventSyncStatus =
    legacy === "in_flight" || legacy === "quarantined" ? legacy : "pending";

  let sequenceNo = asNumber(event.sequence_no, 0);
  if (!Number.isInteger(sequenceNo) || sequenceNo <= 0) {
    sequenceNo = (sequence[key] ?? 0) + 1;
  }
  sequence[key] = Math.max(sequence[key] ?? 0, sequenceNo);

  const severity = event.severity;
  const privacy = event.privacy_classification;
  const normalizedSeverity =
    severity === "warn" || severity === "error" || severity === "critical"
      ? severity
      : "info";
  const retention: RetentionTier =
    event.retention === "critical" || event.retention === "normal" ||
      event.retention === "debug"
      ? event.retention
      : classifyRetention(
        typeof event.event_type === "string" ? event.event_type : "",
        normalizedSeverity,
      );
  return {
    event_id: event.event_id,
    event_type: typeof event.event_type === "string" && event.event_type
      ? event.event_type
      : "client_event",
    task_id: typeof event.task_id === "string" ? event.task_id : null,
    run_id: runId,
    runtime_manifest_id:
      typeof event.runtime_manifest_id === "string"
        ? event.runtime_manifest_id
        : null,
    timestamp: asNumber(event.timestamp, 0),
    clock_source: "device",
    severity: normalizedSeverity,
    payload:
      event.payload && typeof event.payload === "object" ? event.payload : {},
    privacy_classification:
      privacy && PRIVACY_LEVELS.includes(privacy) ? privacy : "standard",
    uploadable: typeof event.uploadable === "boolean"
      ? event.uploadable
      : true,
    retention,
    sync_status: SYNC_STATES.includes(syncStatus) ? syncStatus : "pending",
    sequence_no: sequenceNo,
    seq: asNumber(event.seq, nextSeq()),
    attempts: asNumber(event.attempts, 0),
    last_error: typeof event.last_error === "string" ? event.last_error : null,
    batch_id:
      syncStatus === "in_flight" && typeof event.batch_id === "string"
        ? event.batch_id
        : null,
    in_flight_at:
      syncStatus === "in_flight" ? asNumber(event.in_flight_at, 0) : null,
    quarantined_at:
      syncStatus === "quarantined" ? asNumber(event.quarantined_at, 0) : null,
  };
}

function normalizeInFlight(raw: unknown): InFlightBatch | null {
  if (!raw || typeof raw !== "object") return null;
  const batch = raw as InFlightBatch;
  if (typeof batch.batch_id !== "string" || !batch.batch_id) return null;
  if (!Array.isArray(batch.event_ids)) return null;
  const ids = batch.event_ids.filter(
    (id): id is string => typeof id === "string" && Boolean(id),
  );
  if (ids.length === 0) return null;
  return {
    batch_id: batch.batch_id,
    event_ids: ids,
    claimed_at: asNumber(batch.claimed_at, 0),
    attempts: asNumber(batch.attempts, 1),
  };
}

function normalizeMarkers(raw: unknown): DataLossMarker[] {
  if (!Array.isArray(raw)) return [];
  const out: DataLossMarker[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const marker = item as DataLossMarker;
    if (typeof marker.marker_id !== "string" || !marker.marker_id) continue;
    const tiers = marker.tiers && typeof marker.tiers === "object"
      ? marker.tiers
      : { critical: 0, normal: 0, debug: 0 };
    out.push({
      marker_id: marker.marker_id,
      reason: marker.reason === "capacity" ? "capacity" : "expired",
      dropped: asNumber(marker.dropped, 0),
      event_ids: Array.isArray(marker.event_ids)
        ? marker.event_ids.filter((id): id is string => typeof id === "string")
        : [],
      event_types: Array.isArray(marker.event_types)
        ? marker.event_types.filter(
          (name): name is string => typeof name === "string",
        )
        : [],
      tiers: {
        critical: asNumber(tiers.critical, 0),
        normal: asNumber(tiers.normal, 0),
        debug: asNumber(tiers.debug, 0),
      },
      first_seq: typeof marker.first_seq === "number"
        ? marker.first_seq
        : null,
      last_seq: typeof marker.last_seq === "number" ? marker.last_seq : null,
      at: asNumber(marker.at, 0),
    });
  }
  return out;
}

function normalizeReceipts(raw: unknown): LocalSyncReceipt[] {
  if (!Array.isArray(raw)) return [];
  const out: LocalSyncReceipt[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const receipt = item as LocalSyncReceipt;
    if (typeof receipt.receipt_id !== "string" || !receipt.receipt_id) continue;
    out.push({
      receipt_id: receipt.receipt_id,
      batch_id: typeof receipt.batch_id === "string" ? receipt.batch_id : null,
      received_at: asNumber(receipt.received_at, 0),
      recorded_at: asNumber(receipt.recorded_at, 0),
      accepted: asNumber(receipt.accepted, 0),
      duplicates: asNumber(receipt.duplicates, 0),
      rejected: asNumber(receipt.rejected, 0),
      confirmed: asNumber(receipt.confirmed, 0),
      replayed: receipt.replayed === true,
      protocol_version:
        typeof receipt.protocol_version === "string"
          ? receipt.protocol_version
          : null,
    });
  }
  return out.slice(-20);
}

function normalizeState(
  data: Partial<OfflineState> & Record<string, unknown>,
): OfflineState {
  const state: OfflineState = { ...emptyState(), ...data };
  const sequence: Record<string, number> = {};
  const counters = data.next_sequence;
  if (counters && typeof counters === "object") {
    for (const [key, value] of Object.entries(counters)) {
      if (typeof value === "number" && Number.isFinite(value)) {
        sequence[key] = value;
      }
    }
  }

  let localSeq = 0;
  for (const raw of state.queue) {
    localSeq = Math.max(localSeq, asNumber((raw as OfflineEvent)?.seq, 0));
  }
  const nextSeq = () => {
    localSeq += 1;
    return localSeq;
  };

  state.queue = state.queue
    .map((event) => normalizeEvent(event, sequence, nextSeq))
    .filter((event): event is OfflineEvent => event !== null);
  state.dead_letters = state.dead_letters
    .map((letter) => {
      const event = normalizeEvent(letter?.event, sequence, nextSeq);
      if (!event) return null;
      return {
        event,
        reason: typeof letter?.reason === "string" ? letter.reason : "同步失败",
        dead_at: asNumber(letter?.dead_at, 0),
      } as DeadLetter;
    })
    .filter((letter): letter is DeadLetter => letter !== null);
  state.next_sequence = sequence;
  state.in_flight = normalizeInFlight(data.in_flight);
  state.loss_markers = normalizeMarkers(data.loss_markers);
  state.sync_receipts = normalizeReceipts(data.sync_receipts);
  state.sync_failures = Math.max(0, asNumber(data.sync_failures, 0));
  state.last_failure_at = typeof data.last_failure_at === "number"
    ? data.last_failure_at
    : null;
  return state;
}

export function parseState(raw: string | null): OfflineState {
  if (!raw) return emptyState();
  try {
    const data = JSON.parse(raw) as Partial<OfflineState>;
    if (
      !data || data.version !== 1 ||
      !Array.isArray(data.queue) ||
      !Array.isArray(data.dead_letters) ||
      !Array.isArray(data.tasks)
    ) {
      return emptyState();
    }
    return normalizeState(data as Partial<OfflineState> &
      Record<string, unknown>);
  } catch {
    return emptyState();
  }
}

export class OfflineRepository {
  private chain: Promise<unknown> = Promise.resolve();
  private readonly kv: KeyValueStore;
  private readonly key: string;

  constructor(kv: KeyValueStore, key: string = STATE_KEY) {
    this.kv = kv;
    this.key = key;
  }

  async load(): Promise<OfflineState> {
    return parseState(await this.kv.getItem(this.key));
  }

  async save(state: OfflineState): Promise<void> {
    await this.kv.setItem(this.key, JSON.stringify(state));
  }

  /** 串行化读-改-写，避免并发覆盖。mutator 抛错时不落盘。 */
  update<T>(mutator: (state: OfflineState) => T | Promise<T>): Promise<T> {
    const run = async () => {
      const state = await this.load();
      const result = await mutator(state);
      await this.save(state);
      return result;
    };
    const next = this.chain.then(run, run);
    this.chain = next.catch(() => undefined);
    return next;
  }
}
