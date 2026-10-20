import { test } from "node:test";
import assert from "node:assert/strict";

import { utf8Decode } from "../../src/offline/bytes.ts";
import { sha256Hex } from "../../src/offline/sha256.ts";
import { crc32 } from "../../src/offline/zip.ts";
import {
  DiagnosticsExporter,
  EXPORT_FORMAT_VERSION,
  buildDiagnosticsPackage,
} from "../../src/offline/export.ts";
import type {
  DiagnosticsExportSink,
  ExportRequest,
} from "../../src/offline/export.ts";
import type { OfflineEvent, OfflineState } from "../../src/offline/types.ts";

const NOW = 1_700_000_000_000;

function makeEvent(overrides: Partial<OfflineEvent> = {}): OfflineEvent {
  return {
    event_id: "evt_export_00000001",
    event_type: "task_progress",
    task_id: "task_a",
    run_id: "run_a",
    runtime_manifest_id: "run_a",
    timestamp: NOW - 10_000,
    clock_source: "device",
    severity: "info",
    payload: { percent: 50 },
    privacy_classification: "standard",
    uploadable: true,
    retention: "normal",
    sync_status: "pending",
    sequence_no: 1,
    seq: 1,
    attempts: 0,
    ...overrides,
  };
}

function makeState(events: OfflineEvent[]): OfflineState {
  return {
    version: 1,
    queue: events,
    dead_letters: [{
      event: makeEvent({
        event_id: "evt_dead_000000001",
        event_type: "sync_rejected",
        severity: "error",
        seq: 99,
        sequence_no: 7,
        last_error: "400 bad event",
      }),
      reason: "服务端永久拒绝",
      dead_at: NOW - 5_000,
    }],
    tasks: [],
    last_acked_seq: 3,
    next_seq: 100,
    last_sync_at: NOW - 60_000,
    next_sequence: { run_a: 7 },
    in_flight: null,
    loss_markers: [{
      marker_id: "marker_1",
      reason: "expired",
      dropped: 2,
      event_ids: ["evt_lost_1", "evt_lost_2"],
      event_types: ["debug_trace"],
      tiers: { critical: 0, normal: 0, debug: 2 },
      first_seq: 4,
      last_seq: 5,
      at: NOW - 120_000,
    }],
  };
}

function readZip(bytes: Uint8Array): Map<string, Uint8Array> {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const out = new Map<string, Uint8Array>();
  let offset = 0;
  while (offset + 30 <= bytes.length) {
    if (view.getUint32(offset, true) !== 0x04034b50) break;
    const size = view.getUint32(offset + 18, true);
    const nameLength = view.getUint16(offset + 26, true);
    const extraLength = view.getUint16(offset + 28, true);
    const name = utf8Decode(
      bytes.slice(offset + 30, offset + 30 + nameLength),
    );
    const start = offset + 30 + nameLength + extraLength;
    out.set(name, bytes.slice(start, start + size));
    offset = start + size;
  }
  return out;
}

function jsonLines(data: Uint8Array | undefined): unknown[] {
  assert.ok(data);
  const text = utf8Decode(data);
  return text.split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

function jsonOf(data: Uint8Array | undefined): Record<string, unknown> {
  assert.ok(data);
  return JSON.parse(utf8Decode(data)) as Record<string, unknown>;
}

class FakeSink implements DiagnosticsExportSink {
  writes: { name: string; bytes: Uint8Array }[] = [];
  delivered: { path: string; name: string }[] = [];
  removed: string[] = [];
  fail = false;

  async writeTemp(name: string, bytes: Uint8Array): Promise<string> {
    this.writes.push({ name, bytes });
    return `/tmp/${name}`;
  }

  async deliver(path: string, name: string): Promise<void> {
    if (this.fail) throw new Error("交付失败");
    this.delivered.push({ path, name });
  }

  async removeTemp(path: string): Promise<void> {
    this.removed.push(path);
  }
}

test("sha256 and crc32 match standard vectors", () => {
  assert.equal(
    sha256Hex(""),
    "e3b0c44298fc1c149afbf4c8996fb924" +
      "27ae41e4649b934ca495991b7852b855",
  );
  assert.equal(
    sha256Hex("abc"),
    "ba7816bf8f01cfea414140de5dae2223" +
      "b00361a396177a9cb410ff61f20015ad",
  );
  assert.equal(crc32(new TextEncoder().encode("123456789")), 0xcbf43926);
});

test("builds a self-consistent package with manifest and checksums", () => {
  const state = makeState([
    makeEvent(),
    makeEvent({
      event_id: "evt_export_00000002",
      event_type: "task_succeeded",
      task_id: "task_b",
      run_id: "run_b",
      runtime_manifest_id: "run_b",
      payload: { answer: "done" },
      sequence_no: 2,
      seq: 2,
      timestamp: NOW - 8_000,
    }),
  ]);
  const result = buildDiagnosticsPackage(state, {
    now: NOW,
    device: {
      app_version: "0.1.0",
      platform: "android",
      device_model: "test",
    },
    evaluation_results: [{ case_id: "case_1", passed: true }],
  });
  const files = readZip(result.bytes);

  for (const name of [
    "manifest.json", "device-info.json", "runtime-manifests.jsonl",
    "task-runs.jsonl", "events.jsonl", "sync-status.json",
    "evaluation-results.jsonl", "errors/error-details.jsonl",
    "checksums.sha256",
  ]) {
    assert.ok(files.has(name), `missing ${name}`);
  }
  assert.equal(result.file_name, "evoagent-diagnostics-20231114-221320.zip");

  const manifest = jsonOf(files.get("manifest.json"));
  assert.equal(manifest.format_version, EXPORT_FORMAT_VERSION);
  assert.equal(manifest.exported_at, NOW / 1000);
  assert.deepEqual(
    [...(manifest.files as string[])].sort(),
    [...files.keys()].filter((name) =>
      name !== "manifest.json" && name !== "checksums.sha256"
    ).sort(),
  );
  assert.deepEqual(
    (manifest.counts as Record<string, number>).events,
    3,
  );
  assert.equal((manifest.counts as Record<string, number>).errors, 1);

  const events = jsonLines(files.get("events.jsonl"));
  assert.equal(events.length, 3);
  assert.equal((events[0] as Record<string, unknown>).event_id,
    "evt_export_00000001");

  const checksumText = utf8Decode(files.get("checksums.sha256") as Uint8Array)
    .trim().split("\n");
  assert.equal(checksumText.length, files.size - 1);
  for (const line of checksumText) {
    const [digest, name] = line.split(/\s+/);
    assert.equal(digest, sha256Hex(files.get(name) as Uint8Array));
  }

  const runs = jsonLines(files.get("task-runs.jsonl"));
  assert.equal(runs.length, 2);
  const manifests = jsonLines(files.get("runtime-manifests.jsonl"));
  assert.equal(manifests.length, 2);
  const sync = jsonOf(files.get("sync-status.json"));
  assert.equal(sync.checkpoint, 3);
  assert.equal((sync.loss_markers as unknown[]).length, 1);
});

test("filters by task, time range, failure, and capability", () => {
  const state = makeState([
    makeEvent({ event_id: "evt_export_task_a01", task_id: "task_a" }),
    makeEvent({
      event_id: "evt_export_task_b01",
      task_id: "task_b",
      run_id: "run_b",
      seq: 2,
      timestamp: NOW - 30_000,
      payload: { capability: "calendar" },
    }),
    makeEvent({
      event_id: "evt_export_error_01",
      event_type: "tool_failed",
      severity: "error",
      task_id: "task_a",
      seq: 3,
      timestamp: NOW - 20_000,
    }),
  ]);

  const byTask = buildDiagnosticsPackage(state, {
    now: NOW,
    filter: { scope: "task", task_id: "task_a" },
  });
  const taskIds = jsonLines(readZip(byTask.bytes).get("events.jsonl"))
    .map((event) => (event as Record<string, unknown>).task_id);
  assert.deepEqual(taskIds, ["task_a", "task_a", "task_a"]);

  const byTime = buildDiagnosticsPackage(state, {
    now: NOW,
    filter: { scope: "time_range", from: NOW - 25_000, to: NOW - 15_000 },
  });
  assert.deepEqual(
    jsonLines(readZip(byTime.bytes).get("events.jsonl"))
      .map((event) => (event as Record<string, unknown>).event_id),
    ["evt_export_error_01"],
  );

  const byFailed = buildDiagnosticsPackage(state, {
    now: NOW,
    filter: { scope: "failed" },
  });
  const failedIds = jsonLines(readZip(byFailed.bytes).get("events.jsonl"))
    .map((event) => (event as Record<string, unknown>).event_id);
  assert.deepEqual(failedIds, ["evt_export_error_01", "evt_dead_000000001"]);

  const byCapability = buildDiagnosticsPackage(state, {
    now: NOW,
    filter: { capability: "calendar" },
  });
  assert.deepEqual(
    jsonLines(readZip(byCapability.bytes).get("events.jsonl"))
      .map((event) => (event as Record<string, unknown>).event_id),
    ["evt_export_task_b01"],
  );
});

test("redacts sensitive payloads by default and secret keys always", () => {
  const sensitive = makeEvent({
    event_id: "evt_export_sensitive",
    event_type: "device_action",
    privacy_classification: "sensitive",
    payload: { phone: "13800000000", note: "call mom" },
  });
  const withSecret = makeEvent({
    event_id: "evt_export_secret_key",
    seq: 2,
    payload: { api_token: "sk-live-123456789", step: "login" },
  });
  const state = makeState([sensitive, withSecret]);

  const exporter = new DiagnosticsExporter();
  const preview = exporter.preview(state, { now: NOW });
  assert.equal(preview.sensitive_events, 1);
  assert.ok(preview.redacted_payloads >= 2);
  assert.equal(preview.raw_payloads_included, true);
  assert.equal(preview.sensitive_payloads_included, false);

  const hidden = buildDiagnosticsPackage(state, { now: NOW });
  const hiddenEvents = jsonLines(readZip(hidden.bytes).get("events.jsonl"));
  const hiddenSensitive = hiddenEvents.find((event) =>
    (event as Record<string, unknown>).event_id === "evt_export_sensitive"
  ) as Record<string, unknown>;
  assert.equal(
    (hiddenSensitive.payload as Record<string, unknown>).redacted,
    true,
  );
  const hiddenSecret = hiddenEvents.find((event) =>
    (event as Record<string, unknown>).event_id === "evt_export_secret_key"
  ) as Record<string, unknown>;
  assert.equal(
    (hiddenSecret.payload as Record<string, unknown>).api_token,
    "[REDACTED]",
  );
  assert.equal(
    (hiddenSecret.payload as Record<string, unknown>).step,
    "login",
  );

  const included = buildDiagnosticsPackage(state, {
    now: NOW,
    include_sensitive_payloads: true,
  });
  const includedEvents = jsonLines(readZip(included.bytes).get("events.jsonl"));
  const includedSensitive = includedEvents.find((event) =>
    (event as Record<string, unknown>).event_id === "evt_export_sensitive"
  ) as Record<string, unknown>;
  assert.equal(
    (includedSensitive.payload as Record<string, unknown>).phone,
    "13800000000",
  );
  assert.equal(
    (included.manifest.redaction as Record<string, unknown>)
      .sensitive_payloads_included,
    true,
  );

  const metadataOnly = buildDiagnosticsPackage(state, {
    now: NOW,
    include_raw_payloads: false,
  });
  const metadataEvents = jsonLines(
    readZip(metadataOnly.bytes).get("events.jsonl"),
  );
  assert.ok(metadataEvents.every((event) =>
    ((event as Record<string, unknown>).payload as Record<string, unknown>)
      .redacted === true
  ));
});

test("attachments are opt-in, filtered, and redacted by sensitivity", () => {
  const state = makeState([makeEvent()]);
  const request: ExportRequest = {
    now: NOW,
    include_attachments: true,
    attachments: [
      { name: "shot.png", data: "aGVsbG8=" },
      { name: "shot.png", data: "d29ybGQ=" },
      { name: "../evil.txt", data: "aGVsbG8=" },
      { name: "run.exe", data: "aGVsbG8=" },
      { name: "private.txt", data: "c2VjcmV0", sensitive: true },
    ],
  };
  const exporter = new DiagnosticsExporter();
  const preview = exporter.preview(state, request);
  assert.equal(preview.attachments_included, 2);
  assert.equal(preview.attachments_rejected, 2);
  assert.equal(preview.attachments_excluded, 1);
  assert.ok(preview.warnings.length >= 2);

  const files = readZip(exporter.build(state, request).bytes);
  assert.equal(utf8Decode(files.get("attachments/shot.png") as Uint8Array),
    "hello");
  assert.equal(utf8Decode(files.get("attachments/shot-1.png") as Uint8Array),
    "world");
  assert.equal(files.has("attachments/private.txt"), false);
  assert.equal(files.has("attachments/evil.txt"), false);

  const withoutAttachments = readZip(
    exporter.build(state, { now: NOW }).bytes,
  );
  assert.equal(
    [...withoutAttachments.keys()].some((name) =>
      name.startsWith("attachments/")
    ),
    false,
  );

  const withSensitive = readZip(exporter.build(state, {
    ...request,
    include_sensitive_payloads: true,
  }).bytes);
  assert.equal(
    utf8Decode(withSensitive.get("attachments/private.txt") as Uint8Array),
    "secret",
  );
});

test("export is read-only and deterministic for a fixed clock", () => {
  const state = makeState([makeEvent()]);
  const before = JSON.stringify(state);
  const first = buildDiagnosticsPackage(state, { now: NOW });
  const second = buildDiagnosticsPackage(state, { now: NOW });
  assert.equal(JSON.stringify(state), before);
  assert.equal(first.file_name, second.file_name);
  assert.deepEqual([...first.bytes], [...second.bytes]);
});

test("exportTo cleans up the temp file when delivery fails", async () => {
  const state = makeState([makeEvent()]);
  const exporter = new DiagnosticsExporter();
  const sink = new FakeSink();
  const result = await exporter.exportTo(sink, state, { now: NOW });
  assert.equal(sink.writes.length, 1);
  assert.deepEqual(sink.delivered, [{
    path: `/tmp/${result.file_name}`,
    name: result.file_name,
  }]);
  assert.equal(sink.removed.length, 0);

  const failing = new FakeSink();
  failing.fail = true;
  await assert.rejects(
    () => exporter.exportTo(failing, state, { now: NOW }),
    /交付失败/,
  );
  assert.deepEqual(failing.removed, [`/tmp/${result.file_name}`]);
  assert.equal(failing.delivered.length, 0);
});
