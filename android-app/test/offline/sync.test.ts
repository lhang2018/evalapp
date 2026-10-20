import { test } from "node:test";
import assert from "node:assert/strict";

import { MemoryStore, OfflineRepository } from "../../src/offline/storage.ts";
import { EventQueue } from "../../src/offline/queue.ts";
import { SyncEngine } from "../../src/offline/sync.ts";
import type { EventSyncTransport, SyncResponse } from "../../src/offline/sync.ts";
import { LocalTaskService } from "../../src/offline/tasks.ts";
import type { TaskTransport } from "../../src/offline/tasks.ts";
import type { OfflineEvent } from "../../src/offline/types.ts";

class FakeEventTransport implements EventSyncTransport {
  calls: OfflineEvent[][] = [];
  mode: "accept" | "fail" | "reject" | "duplicate" = "accept";
  error = new Error("offline");

  async syncEvents(events: OfflineEvent[]): Promise<SyncResponse> {
    this.calls.push(events);
    if (this.mode === "fail") throw this.error;
    const ids = events.map((e) => e.event_id);
    if (this.mode === "duplicate") {
      return { accepted: [], duplicates: ids, rejected: [] };
    }
    if (this.mode === "reject") {
      return {
        accepted: [],
        duplicates: [],
        rejected: ids.map((event_id) => ({ event_id, error: "bad payload" })),
      };
    }
    return { accepted: ids, duplicates: [], rejected: [] };
  }
}

class FakeTaskTransport implements TaskTransport {
  submits: { clientRequestId: string; goal: string }[] = [];
  log: string[] = [];
  failNext = false;
  serverStatus = "SUCCEEDED";
  result: string | null = "done";
  failureReason: string | null = null;

  async submitTask(goal: string, _intent: string, _risk: string,
                   clientRequestId: string) {
    this.log.push("submit");
    this.submits.push({ clientRequestId, goal });
    if (this.failNext) throw new Error("network down");
    return { task_id: `srv_${this.submits.length}` };
  }

  async getTask(_taskId: string) {
    return {
      task_status: this.serverStatus,
      result: this.result,
      failure_reason: this.failureReason,
    };
  }
}

function setup(options: { now?: () => number; maxAttempts?: number } = {}) {
  const kv = new MemoryStore();
  const repo = new OfflineRepository(kv);
  const queue = new EventQueue(repo, options.now);
  const transport = new FakeEventTransport();
  return { kv, repo, queue, transport };
}

test("flush uploads pending events and advances checkpoint", async () => {
  const { kv, repo, queue, transport } = setup();
  await queue.enqueue({ event_type: "one" });
  await queue.enqueue({ event_type: "two" });
  const sync = new SyncEngine(queue, transport);

  const result = await sync.flush(true);
  assert.equal(result.ok, true);
  assert.equal(result.accepted, 2);
  assert.equal(result.skipped, false);
  assert.equal((await queue.list()).length, 0);
  assert.equal(await queue.checkpoint(), 2);
  const state = await repo.load();
  assert.ok(state.last_sync_at !== null);

  await queue.enqueue({ event_type: "three" });
  const second = await sync.flush(true);
  assert.equal(second.accepted, 1);
  assert.equal(transport.calls.length, 2);
});

test("network failure keeps events and backs off before retrying", async () => {
  let now = 1000;
  const { queue, transport } = setup({ now: () => now });
  const key = await queue.enqueue({ event_type: "kept" });
  transport.mode = "fail";
  const sync = new SyncEngine(queue, transport, null, {
    now: () => now,
    jitter: () => 1,
  });

  const failed = await sync.flush(true);
  assert.equal(failed.ok, false);
  assert.match(failed.error ?? "", /offline/);
  assert.equal((await queue.list())[0].event_id, key.event_id);
  assert.equal((await queue.list())[0].attempts, 1);
  assert.equal(sync.nextRetryAt, 1000 + 2000);

  now = 1500;
  const skipped = await sync.flush(false);
  assert.equal(skipped.skipped, true);
  assert.equal(transport.calls.length, 1);

  transport.mode = "accept";
  now = 3000;
  const recovered = await sync.flush(false);
  assert.equal(recovered.ok, true);
  assert.equal((await queue.list()).length, 0);
});

test("rejected events are retried then dead-lettered", async () => {
  const { queue, transport } = setup();
  await queue.enqueue({ event_type: "poison" });
  transport.mode = "reject";
  const sync = new SyncEngine(queue, transport, null, { maxAttempts: 2 });

  const first = await sync.flush(true);
  assert.equal(first.rejected, 1);
  assert.equal(first.dead, 0);
  assert.equal((await queue.list()).length, 1);

  const second = await sync.flush(true);
  assert.equal(second.dead, 1);
  assert.equal((await queue.list()).length, 0);
  const dead = await queue.deadLetters();
  assert.equal(dead.length, 1);
  assert.equal(dead[0].reason, "bad payload");
});

test("duplicate acknowledgements also remove local events", async () => {
  const { queue, transport } = setup();
  await queue.enqueue({ event_type: "retry" });
  transport.mode = "duplicate";
  const sync = new SyncEngine(queue, transport);
  const result = await sync.flush(true);
  assert.equal(result.duplicates, 1);
  assert.equal((await queue.list()).length, 0);
});

test("event sync runs before task submission on reconnect", async () => {
  let now = 5000;
  const kv = new MemoryStore();
  const repo = new OfflineRepository(kv);
  const queue = new EventQueue(repo, () => now);
  const events = new FakeEventTransport();
  const tasksTransport = new FakeTaskTransport();
  const log: string[] = [];
  const originalSync = events.syncEvents.bind(events);
  events.syncEvents = async (batch) => {
    log.push("events");
    return originalSync(batch);
  };
  const tasks = new LocalTaskService(repo, tasksTransport, () => now);
  await queue.enqueue({ event_type: "queued" });
  const task = await tasks.submit("生成报告", "general");
  assert.equal(task.status, "LOCAL_ONLY");

  const sync = new SyncEngine(queue, events, tasks, { now: () => now });
  const result = await sync.flush(true);
  assert.deepEqual(log, ["events"]);
  assert.deepEqual(tasksTransport.log, ["submit"]);
  assert.equal(result.tasks?.submitted, 1);
  const stored = await tasks.get(task.local_task_id);
  assert.equal(stored?.status, "SUBMITTED");
  assert.equal(stored?.server_task_id, "srv_1");
});
