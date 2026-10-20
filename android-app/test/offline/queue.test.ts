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

test("queue keeps FIFO order and dedupes by event_id", async () => {
  const { queue } = setup();
  const first = await queue.enqueue({ event_id: "evt_aaaaaaaaaaaaaaaa", event_type: "a" });
  const second = await queue.enqueue({ event_type: "b" });
  const again = await queue.enqueue({ event_id: first.event_id, event_type: "a" });
  assert.equal(again.event_id, first.event_id);
  const list = await queue.list();
  assert.deepEqual(list.map((e) => e.event_id), [first.event_id, second.event_id]);
  assert.ok(second.seq > first.seq);
});

test("queue rejects new events when capacity reached", async () => {
  const { repo, queue } = setup();
  await repo.update((state) => {
    for (let i = 0; i < QUEUE_CAPACITY; i += 1) {
      state.next_seq += 1;
      const event: OfflineEvent = {
        event_id: `evt_fill_${String(i).padStart(10, "0")}`,
        event_type: "fill",
        timestamp: 1,
        severity: "info",
        payload: {},
        privacy_classification: "standard",
        uploadable: true,
        sync_status: "local",
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

test("checkpoint only advances over the contiguous acked prefix", async () => {
  const { queue } = setup();
  const e1 = await queue.enqueue({ event_type: "one" });
  const e2 = await queue.enqueue({ event_type: "two" });
  const e3 = await queue.enqueue({ event_type: "three" });

  await queue.ack([e1.event_id, e3.event_id]);
  assert.equal(await queue.checkpoint(), 1);

  await queue.ack([e2.event_id]);
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

  await queue.ack([normal.event_id]);
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

test("repeated failures move an event to dead letters", async () => {
  const { queue } = setup();
  const event = await queue.enqueue({ event_type: "flaky" });

  let out = await queue.markFailed([event.event_id], "network down", 2);
  assert.deepEqual(out.dead, []);
  assert.equal((await queue.list())[0].attempts, 1);

  out = await queue.markFailed([event.event_id], "network down", 2);
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

test("corrupted snapshots fall back to an empty state", async () => {
  const { kv, repo } = setup();
  await kv.setItem(STATE_KEY, "{not json");
  const state = await repo.load();
  assert.equal(state.queue.length, 0);
  assert.equal(state.tasks.length, 0);

  await kv.setItem(STATE_KEY, JSON.stringify({ version: 1, queue: [] }));
  assert.equal((await repo.load()).tasks.length, 0);
});
