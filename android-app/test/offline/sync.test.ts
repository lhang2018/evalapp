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
  batchIds: string[] = [];
  mode: "accept" | "fail" | "reject" | "duplicate" | "http400" = "accept";
  error = new Error("offline");

  async syncEvents(
    events: OfflineEvent[],
    batchId?: string,
  ): Promise<SyncResponse> {
    this.calls.push(events);
    this.batchIds.push(batchId ?? "");
    if (this.mode === "fail") throw this.error;
    const ids = events.map((e) => e.event_id);
    if (this.mode === "http400") {
      throw Object.assign(new Error("invalid batch"), {
        status: 400,
        body: {
          rejected: ids.map((event_id) => ({
            event_id,
            error: "schema mismatch",
          })),
        },
      });
    }
    if (this.mode === "duplicate") {
      return {
        accepted: [],
        duplicates: ids,
        rejected: [],
        confirmed: ids,
        protocol_version: "1.0",
      };
    }
    if (this.mode === "reject") {
      return {
        accepted: [],
        duplicates: [],
        rejected: ids.map((event_id) => ({ event_id, error: "bad payload" })),
        confirmed: [],
        unconfirmed: ids,
        protocol_version: "1.0",
      };
    }
    return {
      accepted: ids,
      duplicates: [],
      rejected: [],
      confirmed: ids,
      protocol_version: "1.0",
    };
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
  assert.match(transport.batchIds[0], /^batch_/);
  // 队列分配的稳定 batch_id 随批次上传，事件在传期间带同一批次号
  assert.equal(transport.calls[0][0].batch_id, transport.batchIds[0]);
  const state = await repo.load();
  assert.ok(state.last_sync_at !== null);
  assert.equal(state.in_flight, null);

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

test("permanently rejected events are quarantined, not retried forever", async () => {
  const { queue, transport } = setup();
  const poison = await queue.enqueue({ event_type: "poison" });
  transport.mode = "reject";
  const sync = new SyncEngine(queue, transport);

  const first = await sync.flush(true);
  assert.equal(first.ok, true);
  assert.equal(first.rejected, 1);
  assert.equal(first.quarantined, 1);
  assert.equal(first.dead, 0);
  assert.equal((await queue.unsynced()).length, 0);

  const held = await queue.quarantined();
  assert.equal(held.length, 1);
  assert.equal(held[0].event_id, poison.event_id);
  assert.equal(held[0].last_error, "bad payload");
  // 未确认的隔离事件不允许 checkpoint 越过
  assert.equal((await queue.stats()).checkpoint, 0);

  // 隔离事件必须由用户显式重试，不会被自动无限重试
  assert.equal(transport.calls.length, 1);
  assert.equal(await queue.retryQuarantined(poison.event_id), true);
  transport.mode = "accept";
  const second = await sync.flush(true);
  assert.equal(second.accepted, 1);
  assert.equal((await queue.quarantined()).length, 0);
});

test("HTTP 400 rejection details quarantine the batch", async () => {
  const { queue, transport } = setup();
  await queue.enqueue({ event_type: "bad_shape" });
  transport.mode = "http400";
  const sync = new SyncEngine(queue, transport);

  const result = await sync.flush(true);
  assert.equal(result.ok, true);
  assert.equal(result.rejected, 1);
  assert.equal(result.quarantined, 1);
  assert.equal(result.error, undefined);
  assert.equal(sync.nextRetryAt, 0);
  assert.equal((await queue.quarantined())[0].last_error, "schema mismatch");
});

test("a persisted in-flight batch is recovered on the next session", async () => {
  let now = 1000;
  const { queue, transport } = setup({ now: () => now });
  const event = await queue.enqueue({ event_type: "interrupted" });
  await queue.claimBatch(10, 60_000);
  assert.equal((await queue.stats()).inFlight, 1);

  now = 1500;
  const sync = new SyncEngine(queue, transport, null, { now: () => now });
  const result = await sync.flush(true);
  assert.equal(result.recovered, 1);
  assert.equal(result.accepted, 1);
  // event_id 不变，服务端幂等去重保证安全重传
  assert.equal(transport.calls[0][0].event_id, event.event_id);
  assert.equal((await queue.list()).length, 0);
  assert.equal((await queue.stats()).inFlight, 0);
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
