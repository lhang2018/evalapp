import { test } from "node:test";
import assert from "node:assert/strict";

import { MemoryStore, OfflineRepository, STATE_KEY } from "../../src/offline/storage.ts";
import { EventQueue, QUEUE_CAPACITY } from "../../src/offline/queue.ts";
import type { OfflineEvent } from "../../src/offline/types.ts";

function setup(now = () => 1_700_000_000_000) {
  const kv = new MemoryStore();
  const repo = new OfflineRepository(kv);
  const queue = new EventQueue(repo, now);
  return { kv, repo, queue };
}

test("queue keeps FIFO order, dedupes, and numbers events per run", async () => {
  const { queue } = setup();
  const first = await queue.enqueue({
    event_id: "evt_aaaaaaaaaaaaaaaa",
    event_type: "a",
    task_id: "task_alpha",
  });
  const second = await queue.enqueue({
    event_type: "b",
    task_id: "task_alpha",
  });
  const otherRun = await queue.enqueue({
    event_type: "c",
    task_id: "task_beta",
  });
  const clientOnly = await queue.enqueue({ event_type: "client_event" });
  const again = await queue.enqueue({
    event_id: first.event_id,
    event_type: "a",
  });

  assert.equal(again.event_id, first.event_id);
  const list = await queue.list();
  assert.deepEqual(
    list.map((e) => e.event_id),
    [first.event_id, second.event_id, otherRun.event_id, clientOnly.event_id],
  );
  assert.ok(second.seq > first.seq);
  assert.deepEqual(
    [first.sequence_no, second.sequence_no, otherRun.sequence_no,
      clientOnly.sequence_no],
    [1, 2, 1, 1],
  );
  assert.equal(first.run_id, "task_alpha");
  assert.equal(otherRun.run_id, "task_beta");
  assert.equal(clientOnly.run_id, null);
  assert.equal(first.clock_source, "device");
});

test("queue rejects new events when capacity reached", async () => {
  const { repo, queue } = setup();
  await repo.update((state) => {
    for (let i = 0; i < QUEUE_CAPACITY; i += 1) {
      state.next_seq += 1;
      const event: OfflineEvent = {
        event_id: `evt_fill_${String(i).padStart(10, "0")}`,
        event_type: "fill",
        task_id: null,
        run_id: null,
        runtime_manifest_id: null,
        timestamp: 1,
        clock_source: "device",
        severity: "info",
        payload: {},
        privacy_classification: "standard",
        uploadable: true,
        sync_status: "pending",
        sequence_no: i + 1,
        seq: state.next_seq,
        attempts: 0,
      };
      state.queue.push(event);
    }
  });
  await assert.rejects(
    () => queue.enqueue({ event_type: "overflow" }),
    /队列已满/,
  );
});

test("claim marks events in flight and stale batches are recovered", async () => {
  let now = 1_700_000_000_000;
  const { queue } = setup(() => now);
  const e1 = await queue.enqueue({ event_type: "one" });
  const e2 = await queue.enqueue({ event_type: "two" });
  const e3 = await queue.enqueue({ event_type: "three" });

  const first = await queue.claimBatch(2, 60_000);
  assert.ok(first);
  assert.deepEqual(first!.events.map((e) => e.event_id), [
    e1.event_id,
    e2.event_id,
  ]);
  assert.match(first!.batch_id, /^batch_/);
  assert.equal(first!.recovered, 0);
  assert.deepEqual(
    (await queue.unsynced()).map((e) => e.event_id),
    [e3.event_id],
  );
  assert.equal(await queue.claimBatch(2, 60_000), null);

  now += 61_000;
  const retried = await queue.claimBatch(2, 60_000);
  assert.ok(retried);
  assert.equal(retried!.recovered, 2);
  assert.deepEqual(retried!.events.map((e) => e.event_id), [
    e1.event_id,
    e2.event_id,
  ]);
  assert.notEqual(retried!.batch_id, first!.batch_id);
  assert.ok(retried!.events.every((e) => e.batch_id === retried!.batch_id));
});

test("checkpoint only advances over confirmed continuous prefix", async () => {
  const { queue } = setup();
  const e1 = await queue.enqueue({ event_type: "one" });
  const e2 = await queue.enqueue({ event_type: "two" });
  const e3 = await queue.enqueue({ event_type: "three" });

  const batch = await queue.claimBatch(3);
  assert.ok(batch);
  await queue.acknowledgeBatch(batch!.batch_id, [e1.event_id, e3.event_id]);
  assert.equal(await queue.checkpoint(), 1);

  await queue.acknowledgeBatch(batch!.batch_id, [e2.event_id]);
  assert.equal(await queue.checkpoint(), 3);
  assert.equal((await queue.list()).length, 0);
});

test("sensitive events stay local and are never uploaded", async () => {
  const { queue } = setup();
  const sensitive = await queue.enqueue({
    event_type: "device_action",
    payload: { target: "com.example" },
  });
  const personal = await queue.enqueue({
    event_type: "task_queued",
    payload: { email: "a@b.c" },
  });
  const normal = await queue.enqueue({ event_type: "task_queued" });

  assert.equal(sensitive.uploadable, false);
  assert.equal(personal.privacy_classification, "sensitive");
  assert.equal(personal.uploadable, false);

  const pending = await queue.unsynced();
  assert.deepEqual(pending.map((e) => e.event_id), [normal.event_id]);
  const stats = await queue.stats();
  assert.equal(stats.pending, 1);
  assert.equal(stats.localOnly, 2);

  const batch = await queue.claimBatch(10);
  assert.deepEqual(batch!.events.map((e) => e.event_id), [normal.event_id]);
  await queue.acknowledgeBatch(batch!.batch_id, [normal.event_id]);
  // 不可上传事件不阻塞 checkpoint
  assert.equal(await queue.checkpoint(), normal.seq);
});

test("secret payloads are rejected before entering the queue", async () => {
  const { queue } = setup();
  await assert.rejects(
    () => queue.enqueue({
      event_type: "task_queued",
      payload: { api_key: "sk-123456" },
    }),
    /禁止入队/,
  );
});

test("rejected events are quarantined and stay reviewable", async () => {
  const { queue } = setup();
  const bad = await queue.enqueue({ event_type: "poison" });
  const good = await queue.enqueue({ event_type: "fine" });

  const batch = await queue.claimBatch(10);
  assert.ok(batch);
  await queue.acknowledgeBatch(batch!.batch_id, [good.event_id]);
  const count = await queue.quarantineBatch(batch!.batch_id, [
    { event_id: bad.event_id, error: "bad payload" },
  ]);
  assert.equal(count, 1);
  assert.equal((await queue.unsynced()).length, 0);

  const held = await queue.quarantined();
  assert.equal(held.length, 1);
  assert.equal(held[0].event_id, bad.event_id);
  assert.equal(held[0].last_error, "bad payload");
  const stats = await queue.stats();
  assert.equal(stats.quarantined, 1);
  // 未确认的隔离事件阻断连续 checkpoint
  assert.equal(stats.checkpoint, 0);

  assert.equal(await queue.retryQuarantined(bad.event_id), true);
  const restored = await queue.unsynced();
  assert.equal(restored.length, 1);
  assert.equal(restored[0].sync_status, "pending");
  assert.equal(restored[0].last_error, null);
});

test("repeated failures move an event to dead letters", async () => {
  const { queue } = setup();
  const event = await queue.enqueue({ event_type: "flaky" });

  let batch = await queue.claimBatch(10);
  let out = await queue.releaseBatch(batch!.batch_id, "network down", 2);
  assert.deepEqual(out.dead, []);
  assert.equal(out.released, 1);
  assert.equal((await queue.list())[0].attempts, 1);
  assert.equal((await queue.list())[0].sync_status, "pending");

  batch = await queue.claimBatch(10);
  out = await queue.releaseBatch(batch!.batch_id, "network down", 2);
  assert.deepEqual(out.dead, [event.event_id]);
  assert.equal((await queue.list()).length, 0);
  const dead = await queue.deadLetters();
  assert.equal(dead.length, 1);
  assert.equal(dead[0].reason, "network down");

  assert.equal(await queue.retryDeadLetter(event.event_id), true);
  const restored = await queue.list();
  assert.equal(restored.length, 1);
  assert.equal(restored[0].attempts, 0);
});

test("legacy snapshots migrate to the sync state machine", async () => {
  const { kv, repo } = setup();
  await kv.setItem(STATE_KEY, JSON.stringify({
    version: 1,
    queue: [{
      event_id: "evt_legacy_0000000001",
      event_type: "legacy",
      timestamp: 100,
      severity: "info",
      payload: {},
      privacy_classification: "standard",
      uploadable: true,
      sync_status: "local",
      seq: 1,
      attempts: 0,
    }],
    dead_letters: [],
    tasks: [],
    last_acked_seq: 0,
    next_seq: 1,
    last_sync_at: null,
  }));

  const state = await repo.load();
  const event = state.queue[0];
  assert.equal(event.sync_status, "pending");
  assert.equal(event.sequence_no, 1);
  assert.equal(event.run_id, null);
  assert.equal(event.clock_source, "device");
  assert.deepEqual(state.next_sequence, { client: 1 });
  assert.equal(state.in_flight, null);
});

test("corrupted snapshots fall back to an empty state", async () => {
  const { kv, repo } = setup();
  await kv.setItem(STATE_KEY, "{not json");
  const state = await repo.load();
  assert.equal(state.queue.length, 0);
  assert.equal(state.tasks.length, 0);

  await kv.setItem(STATE_KEY, JSON.stringify({ version: 1, queue: [] }));
  assert.equal((await repo.load()).tasks.length, 0);
});
