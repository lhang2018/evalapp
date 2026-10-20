import { test } from "node:test";
import assert from "node:assert/strict";

import { ClientTransport } from "../../src/offline/apiTransport.ts";
import type { OfflineEvent } from "../../src/offline/types.ts";

function event(timestamp: number): OfflineEvent {
  return {
    event_id: "evt_0000000000000001",
    event_type: "task_queued",
    task_id: "local_1",
    timestamp,
    severity: "info",
    payload: {},
    privacy_classification: "standard",
    uploadable: true,
    sync_status: "local",
    seq: 1,
    attempts: 0,
  };
}

function transportWith(captured: unknown[]) {
  return new ClientTransport({
    async syncEvents(events) {
      captured.push(...events);
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
