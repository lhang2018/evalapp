import { test } from "node:test";
import assert from "node:assert/strict";

import {
  BACKGROUND_SYNC_TASK,
  DEFAULT_BACKGROUND_INTERVAL_MINUTES,
  MAX_BACKGROUND_INTERVAL_MINUTES,
  MIN_BACKGROUND_INTERVAL_MINUTES,
  normalizeBackgroundInterval,
  runBackgroundSync,
} from "../../src/offline/background.ts";

function clock() {
  let value = 1_700_000_000_000;
  return () => {
    value += 5;
    return value;
  };
}

test("offline health check skips sync without throwing", async () => {
  const calls: string[] = [];
  const result = await runBackgroundSync({
    health: async () => {
      calls.push("health");
      throw new Error("network down");
    },
    flush: async () => {
      calls.push("flush");
    },
    refresh: async () => {
      calls.push("refresh");
    },
    now: clock(),
  });

  assert.deepEqual(calls, ["health"]);
  assert.equal(result.task, BACKGROUND_SYNC_TASK);
  assert.equal(result.ran, false);
  assert.equal(result.online, false);
  assert.equal(result.refreshed, false);
  assert.match(String(result.error), /network down/);
});

test("online run flushes events and tasks before refreshing", async () => {
  const calls: string[] = [];
  const result = await runBackgroundSync({
    health: async () => {
      calls.push("health");
    },
    flush: async () => {
      calls.push("flush");
    },
    refresh: async () => {
      calls.push("refresh");
    },
    now: clock(),
  });

  assert.deepEqual(calls, ["health", "flush", "refresh"]);
  assert.equal(result.ran, true);
  assert.equal(result.online, true);
  assert.equal(result.refreshed, true);
  assert.equal(result.error, null);
  assert.ok(result.finished_at > result.started_at);
});

test("flush failure is reported and refresh is skipped", async () => {
  const calls: string[] = [];
  const result = await runBackgroundSync({
    health: async () => {
      calls.push("health");
    },
    flush: async () => {
      calls.push("flush");
      throw new Error("server 500");
    },
    refresh: async () => {
      calls.push("refresh");
    },
    now: clock(),
  });

  assert.deepEqual(calls, ["health", "flush"]);
  assert.equal(result.ran, true);
  assert.equal(result.online, true);
  assert.equal(result.refreshed, false);
  assert.match(String(result.error), /server 500/);
});

test("refresh failure never rejects the headless task", async () => {
  const result = await runBackgroundSync({
    health: async () => undefined,
    flush: async () => undefined,
    refresh: async () => {
      throw new Error("status timeout");
    },
    now: clock(),
  });

  assert.equal(result.ran, true);
  assert.equal(result.refreshed, false);
  assert.match(String(result.error), /status timeout/);
});

test("background interval is clamped and rounded", () => {
  assert.equal(normalizeBackgroundInterval(5), MIN_BACKGROUND_INTERVAL_MINUTES);
  assert.equal(
    normalizeBackgroundInterval(10_000),
    MAX_BACKGROUND_INTERVAL_MINUTES,
  );
  assert.equal(normalizeBackgroundInterval(29.6), 30);
  assert.equal(
    normalizeBackgroundInterval(Number.NaN),
    DEFAULT_BACKGROUND_INTERVAL_MINUTES,
  );
});
