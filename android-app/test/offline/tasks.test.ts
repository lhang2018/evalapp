import { test } from "node:test";
import assert from "node:assert/strict";

import { MemoryStore, OfflineRepository } from "../../src/offline/storage.ts";
import { EventQueue } from "../../src/offline/queue.ts";
import { LocalTaskService, mapServerStatus } from "../../src/offline/tasks.ts";
import type { TaskTransport } from "../../src/offline/tasks.ts";
import type { UserInputRequest } from "../../src/offline/types.ts";

class FakeTaskTransport implements TaskTransport {
  submits: { clientRequestId: string; goal: string }[] = [];
  failNext = false;
  serverStatus = "SUCCEEDED";
  result: string | null = "服务端结果";
  failureReason: string | null = null;
  runId: string | null = null;
  waitingRequest: UserInputRequest | null = null;
  waitingApprovals: string[] = [];

  async submitTask(goal: string, _intent: string, _risk: string,
                   clientRequestId: string) {
    this.submits.push({ clientRequestId, goal });
    if (this.failNext) throw new Error("network down");
    return { task_id: `srv_${this.submits.length}` };
  }

  async getTask(_taskId: string) {
    return {
      task_status: this.serverStatus,
      result: this.result,
      failure_reason: this.failureReason,
      current_run_id: this.runId,
      waiting_request: this.waitingRequest,
      waiting_approvals: this.waitingApprovals,
    };
  }
}

function setup(now = () => 1_700_000_000_000) {
  const kv = new MemoryStore();
  const repo = new OfflineRepository(kv);
  const transport = new FakeTaskTransport();
  const tasks = new LocalTaskService(repo, transport, now);
  const queue = new EventQueue(repo, now);
  return { kv, repo, transport, tasks, queue };
}

test("offline time_query completes locally without server submission", async () => {
  const { tasks, transport, queue, repo } = setup();
  const task = await tasks.submit("现在几点了", "time_query");

  assert.equal(task.status, "DONE");
  assert.match(task.result ?? "", /本地时间/);
  assert.equal(task.server_task_id, null);
  assert.equal(transport.submits.length, 0);

  const events = await queue.list();
  assert.equal(events.length, 1);
  assert.equal(events[0].event_type, "task_completed_local");
  assert.equal(events[0].uploadable, true);
  const state = await repo.load();
  assert.equal(state.tasks.length, 1);
});

test("offline submission is stored with a stable idempotency key", async () => {
  const { tasks, transport, queue } = setup();
  const task = await tasks.submit("写入报告", "general");

  assert.equal(task.status, "LOCAL_ONLY");
  assert.match(task.client_request_id, /^req_/);
  assert.equal(transport.submits.length, 0);
  assert.equal((await queue.list())[0].event_type, "task_queued");
});

test("flushPending submits with the same client_request_id across retries", async () => {
  const { tasks, transport } = setup();
  const task = await tasks.submit("写入报告", "general");

  transport.failNext = true;
  const first = await tasks.flushPending();
  assert.equal(first.failed, 1);
  let stored = await tasks.get(task.local_task_id);
  assert.equal(stored?.status, "LOCAL_ONLY");
  assert.match(stored?.last_sync_error ?? "", /network down/);
  const keyAfterFailure = transport.submits[0].clientRequestId;
  assert.equal(keyAfterFailure, task.client_request_id);

  transport.failNext = false;
  const second = await tasks.flushPending();
  assert.equal(second.submitted, 1);
  stored = await tasks.get(task.local_task_id);
  assert.equal(stored?.status, "SUBMITTED");
  assert.equal(stored?.server_task_id, "srv_2");
  assert.equal(transport.submits[1].clientRequestId, keyAfterFailure);
});

test("interrupted submissions require explicit reconciliation", async () => {
  const { repo, tasks } = setup();
  const task = await tasks.submit("写入报告", "general");
  await repo.update((state) => {
    const target = state.tasks.find(
      (t) => t.local_task_id === task.local_task_id,
    );
    if (target) target.status = "SUBMITTING";
  });

  assert.equal(await tasks.recoverInterrupted(), 1);
  let stored = await tasks.get(task.local_task_id);
  assert.equal(stored?.status, "NEEDS_RECONCILE");

  assert.equal(await tasks.confirmResubmit(task.local_task_id), true);
  stored = await tasks.get(task.local_task_id);
  assert.equal(stored?.status, "LOCAL_ONLY");
  assert.equal(await tasks.confirmResubmit(task.local_task_id), false);
});

test("refresh maps server statuses into local states", async () => {
  const { tasks, transport } = setup();
  const task = await tasks.submit("写入报告", "general");
  await tasks.flushPending();

  transport.serverStatus = "RUNNING";
  await tasks.refresh();
  assert.equal((await tasks.get(task.local_task_id))?.status, "SUBMITTED");

  transport.serverStatus = "WAITING_USER";
  await tasks.refresh();
  assert.equal(
    (await tasks.get(task.local_task_id))?.status,
    "WAITING_DEPENDENCY",
  );

  transport.serverStatus = "SUCCEEDED";
  transport.result = "完成";
  await tasks.refresh();
  const done = await tasks.get(task.local_task_id);
  assert.equal(done?.status, "DONE");
  assert.equal(done?.result, "完成");
});

test("failed server tasks become DONE with a failure reason", async () => {
  const { tasks, transport } = setup();
  const task = await tasks.submit("写入报告", "general");
  await tasks.flushPending();
  transport.serverStatus = "FAILED";
  transport.failureReason = "工具失败";
  await tasks.refresh();
  const stored = await tasks.get(task.local_task_id);
  assert.equal(stored?.status, "DONE");
  assert.equal(stored?.failure_reason, "工具失败");
});

test("mapServerStatus covers terminal and waiting states", () => {
  assert.equal(mapServerStatus("SUCCEEDED"), "DONE");
  assert.equal(mapServerStatus("CANCELLED"), "DONE");
  assert.equal(mapServerStatus("SUSPENDED"), "WAITING_DEPENDENCY");
  assert.equal(mapServerStatus("READY"), "SUBMITTED");
});

test("clarification and run identity persist across client restart and clear on resume", async () => {
  const { tasks, transport, kv } = setup();
  const task = await tasks.submit("写入报告", "general");
  await tasks.flushPending();
  transport.serverStatus = "WAITING_USER";
  transport.runId = "run_1";
  transport.waitingRequest = {
    request_id: "question_1", question: "报告保存到哪里？",
    phase: "react", answered: false, created_at: 1,
  };
  await tasks.refresh();
  const restarted = new LocalTaskService(new OfflineRepository(kv), transport);
  const waiting = await restarted.get(task.local_task_id);
  assert.equal(waiting?.server_status, "WAITING_USER");
  assert.equal(waiting?.waiting_request?.question, "报告保存到哪里？");
  assert.equal(waiting?.current_run_id, "run_1");
  transport.serverStatus = "RUNNING";
  transport.runId = "run_2";
  transport.waitingRequest = null;
  await restarted.refresh();
  const resumed = await restarted.get(task.local_task_id);
  assert.equal(resumed?.waiting_request, null);
  assert.equal(resumed?.current_run_id, "run_2");
  assert.equal(resumed?.status, "SUBMITTED");
});

test("pending approval ids persist and clear after task resumes", async () => {
  const { tasks, transport, kv } = setup();
  const task = await tasks.submit("写入报告", "general");
  await tasks.flushPending();
  transport.serverStatus = "SUSPENDED";
  transport.waitingApprovals = ["apr_first", "apr_second"];
  await tasks.refresh();
  const restarted = new LocalTaskService(new OfflineRepository(kv), transport);
  assert.deepEqual((await restarted.get(task.local_task_id))?.waiting_approvals,
    ["apr_first", "apr_second"]);
  transport.serverStatus = "RUNNING";
  transport.waitingApprovals = [];
  await restarted.refresh();
  assert.deepEqual((await restarted.get(task.local_task_id))?.waiting_approvals, []);
});
