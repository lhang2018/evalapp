import { test } from "node:test";
import assert from "node:assert/strict";

import {
  containsSecretPayload,
  toEvidenceEvent,
} from "../../src/offline/evidence.ts";
import type { OfflineEvent } from "../../src/offline/types.ts";

function event(overrides: Partial<OfflineEvent> = {}): OfflineEvent {
  return {
    event_id: "evt_1",
    event_type: "tool_call",
    task_id: "t1",
    run_id: "run_1",
    runtime_manifest_id: "manifest_1",
    timestamp: 1700000000_000,
    clock_source: "device",
    severity: "info",
    payload: { tool: "current_time" },
    privacy_classification: "standard",
    uploadable: true,
    retention: "normal",
    attachment_refs: [],
    sync_status: "pending",
    seq: 1,
    ...overrides,
  } as OfflineEvent;
}

test("普通事件投影为 internal/trusted，正文保留", () => {
  const ev = toEvidenceEvent(event());
  assert.equal(ev.origin, "device");
  assert.equal(ev.trust, "trusted");
  assert.equal(ev.privacy_level, "internal");
  assert.equal(ev.fault_level, "none");
  assert.deepEqual(ev.payload, { tool: "current_time" });
  assert.equal(ev.redacted, false);
  assert.equal(ev.refs.task_id, "t1");
  assert.equal(ev.timestamp, 1700000000);
});

test("敏感事件类型 restricted+redacted，正文不复制", () => {
  const ev = toEvidenceEvent(event({
    event_type: "device_action",
    privacy_classification: "sensitive",
    payload: { target: "设置", args: { text: "你好" } },
  }));
  assert.equal(ev.privacy_level, "restricted");
  assert.equal(ev.redacted, true);
  assert.deepEqual(ev.payload, {});
  assert.equal(ev.payload_ref, "evt_1");
});

test("未知隐私分级 fail-safe 到 restricted", () => {
  const ev = toEvidenceEvent(event({
    privacy_classification: "mystery" as OfflineEvent["privacy_classification"],
  }));
  assert.equal(ev.privacy_level, "restricted");
  assert.equal(ev.redacted, true);
});

test("密钥载荷拒绝入证据流", () => {
  assert.ok(containsSecretPayload({ note: "sk-abcdef0123456789abcdef" }));
  assert.throws(
    () => toEvidenceEvent(event({
      payload: { api_key: "supersecretvalue123" },
    })),
    /拒绝入证据流/,
  );
});

test("故障分级推断覆盖 critical/error/warn/unknown", () => {
  assert.equal(toEvidenceEvent(event({ severity: "critical" })).fault_level,
    "fatal");
  assert.equal(toEvidenceEvent(event({ severity: "error" })).fault_level,
    "degraded");
  assert.equal(toEvidenceEvent(event({ severity: "warn" })).fault_level,
    "transient");
  assert.equal(
    toEvidenceEvent(event({ event_type: "side_effect_unknown" })).fault_level,
    "degraded",
  );
});
