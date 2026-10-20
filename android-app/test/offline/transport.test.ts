import { test } from "node:test";
import assert from "node:assert/strict";

import { ClientTransport } from "../../src/offline/apiTransport.ts";
import type { OfflineEvent } from "../../src/offline/types.ts";

function event(timestamp: number): OfflineEvent {
  return {
    event_id: "evt_0000000000000001",
    event_type: "task_queued",
    task_id: "local_1",
    run_id: "run_local_1",
    runtime_manifest_id: "client-manifest-0.1.0",
    timestamp,
    clock_source: "device",
    severity: "info",
    payload: {},
    privacy_classification: "standard",
    uploadable: true,
    sync_status: "pending",
    sequence_no: 7,
    seq: 1,
    attempts: 0,
  };
}

function transportWith(
  captured: unknown[],
  metadata: unknown[] = [],
) {
  return new ClientTransport({
    async syncEvents(events, meta) {
      captured.push(...events);
      metadata.push(meta);
      return { accepted: [], duplicates: [], rejected: [] };
    },
    async submitTask() {
      return { task_id: "t1" };
    },
    async getTask() {
      return { task_status: "SUCCEEDED" };
    },
  });
}

test("millisecond timestamps are converted to seconds", async () => {
  const captured: unknown[] = [];
  await transportWith(captured).syncEvents([event(1_790_000_000_123)]);
  assert.equal(
    (captured[0] as { timestamp: number }).timestamp,
    1_790_000_000.123,
  );
});

test("second timestamps pass through unchanged", async () => {
  const captured: unknown[] = [];
  await transportWith(captured).syncEvents([event(1_790_000_000)]);
  assert.equal((captured[0] as { timestamp: number }).timestamp, 1_790_000_000);
});

test("run identity, sequence and manifest are forwarded with the batch id", async () => {
  const captured: unknown[] = [];
  const metadata: unknown[] = [];
  await transportWith(captured, metadata).syncEvents(
    [event(1_790_000_000)],
    "batch_stable_0001",
  );
  const sent = captured[0] as {
    run_id: string;
    sequence_no: number;
    runtime_manifest_id: string;
  };
  assert.equal(sent.run_id, "run_local_1");
  assert.equal(sent.sequence_no, 7);
  assert.equal(sent.runtime_manifest_id, "client-manifest-0.1.0");
  assert.equal(
    (metadata[0] as { batch_id: string }).batch_id,
    "batch_stable_0001",
  );
});
