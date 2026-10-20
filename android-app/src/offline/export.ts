/**
 * 离线诊断包导出（补充 C §7）。
 *
 * 纯函数构建 + 注入式文件交付：导出只读取本地状态，不改变同步状态、
 * 不删除事件，也不触发上传。默认排除敏感 payload 与附件；密钥类字段
 * 即使包含原始内容也会被二次脱敏。
 */

import { base64Decode, utf8Encode } from "./bytes.ts";
import { sha256Hex } from "./sha256.ts";
import { zipStore } from "./zip.ts";
import type { ZipEntry } from "./zip.ts";
import type { OfflineEvent, OfflineState } from "./types.ts";

export const EXPORT_FORMAT_VERSION = "1.0";
export const MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024;
export const MAX_ATTACHMENTS = 100;
export const MAX_EXPORT_BYTES = 100 * 1024 * 1024;

const ATTACHMENT_EXTS = new Set([
  ".png", ".jpg", ".jpeg", ".txt", ".log", ".json", ".jsonl",
]);
const SECRET_KEY_RE =
  /(password|passwd|token|secret|api[_-]?key|authorization|cookie|credit[_-]?card|id[_-]?card|身份证|密码|令牌|密钥)/i;
const SECRET_VALUE_RE = /^(sk-|ghp_|xox[baprs]-|bearer\s)/i;
const FAILED_TYPE_RE = /(fail|error|timeout|crash|rejected)/i;

export type ExportScope = "all" | "task" | "time_range" | "failed";

export interface ExportFilter {
  scope?: ExportScope;
  task_id?: string | null;
  from?: number | null;
  to?: number | null;
  capability?: string | null;
}

export interface ExportAttachment {
  name: string;
  data: string | Uint8Array;
  sensitive?: boolean;
}

export interface ExportDeviceInfo {
  app_version?: string;
  platform?: string;
  device_model?: string;
  os_version?: string;
  installation_id?: string | null;
}

export interface ExportRequest {
  filter?: ExportFilter;
  device?: ExportDeviceInfo;
  attachments?: ExportAttachment[];
  evaluation_results?: unknown[];
  /** 默认 false：敏感事件只导出元数据 */
  include_sensitive_payloads?: boolean;
  /** 默认 true：standard 事件 payload；false 时只导出字段名 */
  include_raw_payloads?: boolean;
  /** 默认 false */
  include_attachments?: boolean;
  /** 测试/去重可注入；默认 Date.now() */
  now?: number;
}

export interface ExportPreview {
  events: number;
  sensitive_events: number;
  failed_events: number;
  attachments_included: number;
  attachments_excluded: number;
  attachments_rejected: number;
  redacted_payloads: number;
  redacted_fields: number;
  raw_payloads_included: boolean;
  sensitive_payloads_included: boolean;
  loss_markers: number;
  warnings: string[];
  categories: string[];
}

export interface ExportManifest {
  format_version: string;
  exported_at: number;
  timestamp_unit: "epoch_seconds";
  filters: {
    scope: ExportScope;
    task_id: string | null;
    from: number | null;
    to: number | null;
    capability: string | null;
    failed_only: boolean;
  };
  files: string[];
  source: ExportDeviceInfo & { app: string };
  counts: {
    events: number;
    dead_letters: number;
    errors: number;
    runtime_manifests: number;
    task_runs: number;
    attachments: number;
    loss_markers: number;
  };
  redaction: {
    raw_payloads_included: boolean;
    sensitive_payloads_included: boolean;
    attachments_included: boolean;
    redacted_payloads: number;
    redacted_fields: number;
  };
}

export interface ExportResult {
  file_name: string;
  bytes: Uint8Array;
  manifest: ExportManifest;
  preview: ExportPreview;
  checksums: Record<string, string>;
}

/** 由 RN 侧实现：先写临时文件，交付成功后清理；失败时不得留下半成品。 */
export interface DiagnosticsExportSink {
  writeTemp(fileName: string, bytes: Uint8Array): Promise<string>;
  deliver(tempPath: string, fileName: string): Promise<void>;
  removeTemp(tempPath: string): Promise<void>;
}

interface PreparedPayload {
  payload: Record<string, unknown>;
  redacted: boolean;
  redactedFields: number;
}

interface PreparedExport {
  selected: OfflineEvent[];
  payloads: Map<string, PreparedPayload>;
  attachments: Map<string, Uint8Array>;
  preview: ExportPreview;
}

function asString(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function isFailedEvent(event: OfflineEvent): boolean {
  return event.severity === "error" || event.severity === "critical" ||
    event.sync_status === "quarantined" ||
    FAILED_TYPE_RE.test(event.event_type);
}

function capabilityOf(event: OfflineEvent): string {
  const payload = event.payload ?? {};
  for (const key of ["capability", "domain", "expert", "skill"]) {
    const value = payload[key];
    if (typeof value === "string") return value.toLowerCase();
  }
  return "";
}

function normalizeFilter(raw?: ExportFilter): ExportFilter {
  const filter = raw ?? {};
  const scope: ExportScope =
    filter.scope === "task" || filter.scope === "time_range" ||
      filter.scope === "failed"
      ? filter.scope
      : filter.task_id
        ? "task"
        : filter.from != null || filter.to != null
          ? "time_range"
          : "all";
  return {
    scope,
    task_id: asString(filter.task_id),
    from: typeof filter.from === "number" ? filter.from : null,
    to: typeof filter.to === "number" ? filter.to : null,
    capability: asString(filter.capability),
  };
}

function matches(event: OfflineEvent, filter: ExportFilter): boolean {
  if (filter.task_id && event.task_id !== filter.task_id) return false;
  if (filter.from != null && event.timestamp < filter.from) return false;
  if (filter.to != null && event.timestamp >= filter.to) return false;
  if (filter.capability) {
    const wanted = filter.capability.toLowerCase();
    if (!capabilityOf(event).includes(wanted)) return false;
  }
  if (filter.scope === "failed") return isFailedEvent(event);
  return true;
}

function collectEvents(state: OfflineState): OfflineEvent[] {
  const seen = new Set<string>();
  const out: OfflineEvent[] = [];
  const push = (event: OfflineEvent | undefined): void => {
    if (!event || seen.has(event.event_id)) return;
    seen.add(event.event_id);
    out.push(event);
  };
  for (const event of state.queue) push(event);
  for (const letter of [...state.dead_letters].sort(
    (a, b) => a.dead_at - b.dead_at,
  )) {
    push(letter.event);
  }
  return out.sort((a, b) =>
    a.seq - b.seq || a.timestamp - b.timestamp ||
    a.event_id.localeCompare(b.event_id)
  );
}

interface RedactionResult {
  value: unknown;
  fields: number;
}

function redactSecrets(value: unknown, depth = 0): RedactionResult {
  if (depth > 8) return { value: "[TRUNCATED]", fields: 0 };
  if (typeof value === "string") {
    return SECRET_VALUE_RE.test(value)
      ? { value: "[REDACTED]", fields: 1 }
      : { value, fields: 0 };
  }
  if (Array.isArray(value)) {
    let fields = 0;
    const items = value.map((item) => {
      const result = redactSecrets(item, depth + 1);
      fields += result.fields;
      return result.value;
    });
    return { value: items, fields };
  }
  if (value && typeof value === "object") {
    let fields = 0;
    const out: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value)) {
      if (SECRET_KEY_RE.test(key)) {
        out[key] = "[REDACTED]";
        fields += 1;
        continue;
      }
      const result = redactSecrets(item, depth + 1);
      fields += result.fields;
      out[key] = result.value;
    }
    return { value: out, fields };
  }
  return { value, fields: 0 };
}

function preparePayload(
  event: OfflineEvent,
  request: ExportRequest,
): PreparedPayload {
  const raw = event.payload ?? {};
  const sensitive = event.privacy_classification !== "standard";
  if (sensitive && !request.include_sensitive_payloads) {
    return {
      payload: {
        redacted: true,
        reason: "sensitive_payload",
        keys: Object.keys(raw).sort(),
      },
      redacted: true,
      redactedFields: Object.keys(raw).length,
    };
  }
  if (request.include_raw_payloads === false) {
    return {
      payload: {
        redacted: true,
        reason: "raw_content_excluded",
        keys: Object.keys(raw).sort(),
      },
      redacted: true,
      redactedFields: Object.keys(raw).length,
    };
  }
  const result = redactSecrets(raw);
  return {
    payload: result.value as Record<string, unknown>,
    redacted: result.fields > 0,
    redactedFields: result.fields,
  };
}

function sanitizeAttachmentName(raw: string): string | null {
  if (raw.includes("/") || raw.includes("\\") || raw.includes("..")) {
    return null;
  }
  const base = raw.trim();
  if (!/^[A-Za-z0-9._-]{1,96}$/.test(base)) return null;
  const dot = base.lastIndexOf(".");
  if (dot <= 0) return null;
  return ATTACHMENT_EXTS.has(base.slice(dot).toLowerCase()) ? base : null;
}

function uniqueName(name: string, used: Set<string>): string {
  if (!used.has(name)) return name;
  const dot = name.lastIndexOf(".");
  const stem = name.slice(0, dot);
  const ext = name.slice(dot);
  let index = 1;
  while (used.has(`${stem}-${index}${ext}`)) index += 1;
  return `${stem}-${index}${ext}`;
}

function prepareExport(
  state: OfflineState,
  request: ExportRequest,
): PreparedExport {
  const filter = normalizeFilter(request.filter);
  const selected = collectEvents(state).filter((event) =>
    matches(event, filter)
  );
  const payloads = new Map<string, PreparedPayload>();
  let redactedPayloads = 0;
  let redactedFields = 0;
  let sensitiveEvents = 0;
  let failedEvents = 0;
  for (const event of selected) {
    if (event.privacy_classification !== "standard") sensitiveEvents += 1;
    if (isFailedEvent(event)) failedEvents += 1;
    const prepared = preparePayload(event, request);
    payloads.set(event.event_id, prepared);
    if (prepared.redacted) redactedPayloads += 1;
    redactedFields += prepared.redactedFields;
  }

  const warnings: string[] = [];
  const attachments = new Map<string, Uint8Array>();
  let excludedAttachments = 0;
  let rejectedAttachments = 0;
  const provided = request.attachments ?? [];
  if (request.include_attachments) {
    let total = 0;
    for (const attachment of provided) {
      if (attachment.sensitive && !request.include_sensitive_payloads) {
        excludedAttachments += 1;
        continue;
      }
      if (attachments.size >= MAX_ATTACHMENTS) {
        rejectedAttachments += 1;
        warnings.push("附件数量超过上限");
        continue;
      }
      const name = sanitizeAttachmentName(attachment.name);
      if (!name) {
        rejectedAttachments += 1;
        warnings.push(`附件名称或类型不允许: ${attachment.name}`);
        continue;
      }
      const data = typeof attachment.data === "string"
        ? base64Decode(attachment.data)
        : attachment.data;
      if (!data || data.length === 0) {
        rejectedAttachments += 1;
        warnings.push(`附件内容为空或不是合法 base64: ${attachment.name}`);
        continue;
      }
      if (data.length > MAX_ATTACHMENT_BYTES) {
        rejectedAttachments += 1;
        warnings.push(`附件超过 20 MiB: ${attachment.name}`);
        continue;
      }
      if (total + data.length > MAX_EXPORT_BYTES) {
        rejectedAttachments += 1;
        warnings.push("附件总量超过导出上限");
        continue;
      }
      total += data.length;
      attachments.set(uniqueName(name, new Set(attachments.keys())), data);
    }
  } else {
    excludedAttachments = provided.length;
  }

  const categories: string[] = [];
  if (sensitiveEvents > 0) categories.push("敏感事件元数据");
  if (failedEvents > 0) categories.push("错误详情");
  if (attachments.size > 0) categories.push(`附件 ${attachments.size} 个`);
  if (request.include_sensitive_payloads) categories.push("敏感原文");
  if (request.include_raw_payloads !== false) categories.push("原始负载");

  const preview: ExportPreview = {
    events: selected.length,
    sensitive_events: sensitiveEvents,
    failed_events: failedEvents,
    attachments_included: attachments.size,
    attachments_excluded: excludedAttachments,
    attachments_rejected: rejectedAttachments,
    redacted_payloads: redactedPayloads,
    redacted_fields: redactedFields,
    raw_payloads_included: request.include_raw_payloads !== false,
    sensitive_payloads_included: request.include_sensitive_payloads === true,
    loss_markers: state.loss_markers.length,
    warnings,
    categories,
  };
  return { selected, payloads, attachments, preview };
}

function jsonLine(value: unknown): string | null {
  try {
    return JSON.stringify(value);
  } catch {
    return null;
  }
}

function lines(values: unknown[]): Uint8Array {
  const parts: string[] = [];
  for (const value of values) {
    const line = jsonLine(value);
    if (line != null) parts.push(line);
  }
  return utf8Encode(parts.length ? `${parts.join("\n")}\n` : "");
}

function eventRecord(
  event: OfflineEvent,
  prepared: PreparedPayload,
): Record<string, unknown> {
  return {
    event_id: event.event_id,
    event_type: event.event_type,
    task_id: event.task_id ?? null,
    run_id: event.run_id ?? null,
    runtime_manifest_id: event.runtime_manifest_id ?? null,
    timestamp: event.timestamp,
    timestamp_unit: "milliseconds",
    clock_source: event.clock_source,
    sequence_no: event.sequence_no,
    severity: event.severity,
    privacy_classification: event.privacy_classification,
    sync_status: event.sync_status,
    retention: event.retention,
    payload: prepared.payload,
    payload_redacted: prepared.redacted,
    attachment_refs: event.attachment_refs,
    last_error: event.last_error ?? null,
  };
}

function buildRuntimeManifests(selected: OfflineEvent[]): Uint8Array {
  const groups = new Map<string, {
    taskIds: Set<string>;
    runIds: Set<string>;
    count: number;
    first: number;
    last: number;
  }>();
  for (const event of selected) {
    const id = event.runtime_manifest_id;
    if (!id) continue;
    const group = groups.get(id) ?? {
      taskIds: new Set<string>(),
      runIds: new Set<string>(),
      count: 0,
      first: event.timestamp,
      last: event.timestamp,
    };
    if (event.task_id) group.taskIds.add(event.task_id);
    if (event.run_id) group.runIds.add(event.run_id);
    group.count += 1;
    group.first = Math.min(group.first, event.timestamp);
    group.last = Math.max(group.last, event.timestamp);
    groups.set(id, group);
  }
  const out: unknown[] = [];
  for (const [id, group] of [...groups.entries()].sort()) {
    out.push({
      runtime_manifest_id: id,
      task_ids: [...group.taskIds].sort(),
      run_ids: [...group.runIds].sort(),
      event_count: group.count,
      first_timestamp: group.first,
      last_timestamp: group.last,
    });
  }
  return lines(out);
}

function buildTaskRuns(selected: OfflineEvent[]): Uint8Array {
  const groups = new Map<string, {
    taskId: string | null;
    runId: string | null;
    count: number;
    first: number;
    last: number;
    severities: Record<string, number>;
    sync: Record<string, number>;
    failed: boolean;
  }>();
  for (const event of selected) {
    const key = event.run_id ?? event.task_id ?? "client";
    const group = groups.get(key) ?? {
      taskId: event.task_id ?? null,
      runId: event.run_id ?? null,
      count: 0,
      first: event.timestamp,
      last: event.timestamp,
      severities: {},
      sync: {},
      failed: false,
    };
    if (!group.taskId && event.task_id) group.taskId = event.task_id;
    if (!group.runId && event.run_id) group.runId = event.run_id;
    group.count += 1;
    group.first = Math.min(group.first, event.timestamp);
    group.last = Math.max(group.last, event.timestamp);
    group.severities[event.severity] =
      (group.severities[event.severity] ?? 0) + 1;
    group.sync[event.sync_status] = (group.sync[event.sync_status] ?? 0) + 1;
    group.failed = group.failed || isFailedEvent(event);
    groups.set(key, group);
  }
  const out: unknown[] = [];
  for (const [key, group] of [...groups.entries()].sort()) {
    out.push({
      run_key: key,
      task_id: group.taskId,
      run_id: group.runId,
      event_count: group.count,
      first_timestamp: group.first,
      last_timestamp: group.last,
      severities: group.severities,
      sync_statuses: group.sync,
      failed: group.failed,
    });
  }
  return lines(out);
}

function buildSyncStatus(state: OfflineState, selected: number): Uint8Array {
  const counts: Record<string, number> = {};
  for (const event of state.queue) {
    counts[event.sync_status] = (counts[event.sync_status] ?? 0) + 1;
  }
  return utf8Encode(JSON.stringify({
    checkpoint: state.last_acked_seq,
    next_seq: state.next_seq,
    in_flight: state.in_flight,
    queue_counts: counts,
    dead_letters: state.dead_letters.length,
    loss_markers: state.loss_markers,
    last_sync_at: state.last_sync_at,
    exported_events: selected,
  }, null, 2));
}

function stamp(now: number): string {
  const date = new Date(now);
  const pad = (value: number): string => String(value).padStart(2, "0");
  return `${date.getUTCFullYear()}${pad(date.getUTCMonth() + 1)}${
    pad(date.getUTCDate())
  }-${pad(date.getUTCHours())}${pad(date.getUTCMinutes())}${
    pad(date.getUTCSeconds())
  }`;
}

export function buildDiagnosticsPackage(
  state: OfflineState,
  request: ExportRequest = {},
): ExportResult {
  const prepared = prepareExport(state, request);
  const now = request.now ?? Date.now();
  const filter = normalizeFilter(request.filter);
  const files = new Map<string, Uint8Array>();

  const eventLines = prepared.selected.map((event) =>
    eventRecord(event, prepared.payloads.get(event.event_id) as PreparedPayload)
  );
  files.set("events.jsonl", lines(eventLines));
  files.set("errors/error-details.jsonl", lines(
    prepared.selected
      .filter(isFailedEvent)
      .map((event) => ({
        ...eventRecord(
          event,
          prepared.payloads.get(event.event_id) as PreparedPayload,
        ),
        last_error: event.last_error ?? null,
      })),
  ));
  files.set("runtime-manifests.jsonl",
    buildRuntimeManifests(prepared.selected));
  files.set("task-runs.jsonl", buildTaskRuns(prepared.selected));
  files.set("sync-status.json",
    buildSyncStatus(state, prepared.selected.length));

  const evaluation: unknown[] = [];
  for (const item of request.evaluation_results ?? []) {
    const result = redactSecrets(item);
    evaluation.push(result.value);
    if (result.fields > 0) {
      prepared.preview.redacted_payloads += 1;
      prepared.preview.redacted_fields += result.fields;
    }
  }
  files.set("evaluation-results.jsonl", lines(evaluation));

  for (const [name, data] of prepared.attachments) {
    files.set(`attachments/${name}`, data);
  }

  const device: ExportDeviceInfo & { app: string } = {
    app: "EvoAgent",
    app_version: request.device?.app_version ?? "unknown",
    platform: request.device?.platform ?? "unknown",
    device_model: request.device?.device_model,
    os_version: request.device?.os_version,
    installation_id: request.device?.installation_id ?? null,
  };
  files.set("device-info.json", utf8Encode(JSON.stringify({
    ...device,
    exported_at: Math.floor(now / 1000),
  }, null, 2)));

  const manifest: ExportManifest = {
    format_version: EXPORT_FORMAT_VERSION,
    exported_at: now / 1000,
    timestamp_unit: "epoch_seconds",
    filters: {
      scope: filter.scope ?? "all",
      task_id: filter.task_id ?? null,
      from: filter.from ?? null,
      to: filter.to ?? null,
      capability: filter.capability ?? null,
      failed_only: filter.scope === "failed",
    },
    files: [...files.keys()].filter((name) => name !== "manifest.json")
      .sort(),
    source: device,
    counts: {
      events: prepared.selected.length,
      dead_letters: state.dead_letters.length,
      errors: prepared.selected.filter(isFailedEvent).length,
      runtime_manifests: countRuntimeManifests(prepared.selected),
      task_runs: countTaskRuns(prepared.selected),
      attachments: prepared.attachments.size,
      loss_markers: state.loss_markers.length,
    },
    redaction: {
      raw_payloads_included: prepared.preview.raw_payloads_included,
      sensitive_payloads_included:
        prepared.preview.sensitive_payloads_included,
      attachments_included: prepared.attachments.size > 0,
      redacted_payloads: prepared.preview.redacted_payloads,
      redacted_fields: prepared.preview.redacted_fields,
    },
  };
  files.set("manifest.json", utf8Encode(JSON.stringify(manifest, null, 2)));

  const checksums: Record<string, string> = {};
  const names = [...files.keys()].sort();
  for (const name of names) {
    checksums[name] = sha256Hex(files.get(name) as Uint8Array);
  }
  const checksumLines = names.map((name) => `${checksums[name]}  ${name}`);
  files.set("checksums.sha256", utf8Encode(`${checksumLines.join("\n")}\n`));

  const entries: ZipEntry[] = [...files.keys()].sort().map((name) => ({
    name,
    data: files.get(name) as Uint8Array,
  }));
  return {
    file_name: `evoagent-diagnostics-${stamp(now)}.zip`,
    bytes: zipStore(entries, new Date(now)),
    manifest,
    preview: prepared.preview,
    checksums,
  };
}

function countRuntimeManifests(selected: OfflineEvent[]): number {
  return new Set(
    selected.map((event) => event.runtime_manifest_id).filter(
      (value): value is string => Boolean(value),
    ),
  ).size;
}

function countTaskRuns(selected: OfflineEvent[]): number {
  return new Set(
    selected.map((event) => event.run_id ?? event.task_id ?? "client"),
  ).size;
}

export class DiagnosticsExporter {
  preview(state: OfflineState, request: ExportRequest = {}): ExportPreview {
    return prepareExport(state, request).preview;
  }

  build(state: OfflineState, request: ExportRequest = {}): ExportResult {
    return buildDiagnosticsPackage(state, request);
  }

  /** 先写临时文件再交付；交付失败清理临时文件，不改变原始状态。 */
  async exportTo(
    sink: DiagnosticsExportSink,
    state: OfflineState,
    request: ExportRequest = {},
  ): Promise<ExportResult> {
    const result = this.build(state, request);
    const tempPath = await sink.writeTemp(result.file_name, result.bytes);
    try {
      await sink.deliver(tempPath, result.file_name);
      return result;
    } catch (error) {
      try {
        await sink.removeTemp(tempPath);
      } catch {
        // 清理失败不应覆盖原始交付错误
      }
      throw error;
    }
  }
}
