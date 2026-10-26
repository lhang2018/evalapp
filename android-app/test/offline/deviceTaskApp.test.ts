import { test } from "node:test";
import assert from "node:assert/strict";
import { DeviceTaskService } from "../../src/features/pipeline/deviceTasks.ts";
import { MemoryStore, OfflineRepository, STATE_KEY } from "../../src/offline/storage.ts";
import { LocalTaskService } from "../../src/offline/tasks.ts";
import { DeviceBroker } from "../../src/device/broker.ts";
import { NativeDeviceTransport } from "../../src/device/nativeTransport.ts";
import { TaskActionLedger } from "../../src/device/taskProtocol.ts";
import { taskViewOf } from "../../src/features/taskview/state.ts";
import { describeTerminalTask } from "../../src/device/notifications.ts";
import { EventQueue, enqueueInto, QUEUE_CAPACITY } from "../../src/offline/queue.ts";
import { SyncEngine } from "../../src/offline/sync.ts";

class Store extends MemoryStore {
  failTaskWrite = false;
  async setItem(key: string, value: string) {
    if (this.failTaskWrite && key === STATE_KEY) throw new Error("task storage failed");
    return super.setItem(key, value);
  }
}

function fixture(store = new Store(), options: { unverified?: boolean; afterDispatch?: () => void } = {}) {
  let dispatches = 0;
  let serverSubmits = 0;
  const repo = new OfflineRepository(store);
  const ledger = new TaskActionLedger(store);
  const snapshot = (package_name: string) => ({ package_name, nodes: [], node_count: 0, truncated: false });
  const broker = new DeviceBroker(new NativeDeviceTransport({ isServiceEnabled: async () => true,
    launchIntent: async () => {
      dispatches++;
      options.afterDispatch?.();
      return { ok: true, evidence: options.unverified ? undefined : {
        before: snapshot("com.example.home"), after: snapshot("com.android.settings"), private_text: "local-secret-screen",
      } };
    }, performClick: async () => { throw new Error("unexpected click"); },
  }));
  const service = new DeviceTaskService(repo, ledger, broker, undefined, "test-manifest");
  const tasks = new LocalTaskService(repo, {
    submitTask: async () => { serverSubmits++; return { task_id: "server" }; },
    getTask: async () => { throw new Error("device task must not be polled on server"); },
  });
  return { store, repo, ledger, broker, service, tasks,
    dispatches: () => dispatches, serverSubmits: () => serverSubmits };
}

test("unknown and compound goals stay on the existing planner path", async () => {
  const f = fixture();
  for (const goal of ["你好", "帮我整理报告", "打开设置然后发送消息", "如何打开设置？"]) {
    assert.equal(await f.service.prepare(goal), null);
  }
  assert.equal((await f.tasks.list()).length, 0);
  assert.equal(f.dispatches(), 0);
});

test("prepare persists exact identity without dispatch or server submission", async () => {
  const f = fixture();
  const task = (await f.service.prepare("帮我打开设置"))!;
  assert.equal(task.execution_target, "device");
  assert.equal(task.device_execution!.proposal.task_id, task.local_task_id);
  assert.equal(task.device_execution!.proposal.run_id, task.current_run_id);
  assert.match(taskViewOf(task).label, /确认本机操作/);
  await f.tasks.flushPending();
  await f.tasks.refresh();
  assert.equal(f.dispatches(), 0);
  assert.equal(f.serverSubmits(), 0);
  assert.equal(await f.ledger.getProposal(task.device_execution!.proposal.action_id), null);
});

test("end-to-end local orchestration separates action verification from user acceptance", async () => {
  const f = fixture();
  const task = (await f.service.prepare("打开设置"))!;
  const id = task.local_task_id;
  const receipt = await f.service.execute(id, async () => true);
  assert.equal(f.dispatches(), 1);
  assert.equal(receipt!.state, "verified");
  assert.equal(receipt!.effect_id, task.device_execution!.proposal.effect_id);
  const after = (await f.tasks.get(id))!;
  assert.equal(after.device_execution!.phase, "awaiting_acceptance");
  assert.notEqual(after.status, "DONE");
  assert.equal(taskViewOf(after).state, "needs_you");
  const events = (await f.repo.load()).queue;
  const uploads = events.filter((row) => row.event_type === "device_action_receipt_verification");
  assert.equal(uploads.length, 1);
  assert.equal(uploads[0].uploadable, true);
  assert.equal(uploads[0].run_id, task.current_run_id);
  assert.equal(uploads[0].payload.effect_id, receipt!.effect_id);
  assert.ok(!JSON.stringify(uploads).includes("local-secret-screen"));
  assert.ok(!("evidence" in uploads[0].payload));
  await assert.rejects(f.service.accept(id, false));
  await f.service.accept(id, true);
  const accepted = (await f.tasks.get(id))!;
  assert.equal(accepted.status, "DONE");
  assert.equal(accepted.server_status, undefined);
  assert.match(taskViewOf(accepted).label, /你已确认/);
  await f.tasks.flushPending();
  assert.equal(f.serverSubmits(), 0);
});

test("refusing authorization leaves the prepared task unclaimed", async () => {
  const f = fixture();
  const task = (await f.service.prepare("打开设置"))!;
  assert.equal(await f.service.execute(task.local_task_id, async () => false), null);
  assert.equal(f.dispatches(), 0);
  assert.equal((await f.tasks.get(task.local_task_id))!.device_execution!.phase, "prepared");
  await assert.rejects(f.service.accept(task.local_task_id, true));
});

test("restart and repeated refresh never dispatch again or enqueue duplicate receipts", async () => {
  const original = fixture();
  const task = (await original.service.prepare("打开设置"))!;
  await original.service.execute(task.local_task_id, async () => true);
  const restarted = fixture(original.store);
  const cached = await restarted.service.execute(task.local_task_id, async () => { throw new Error("no new confirmation"); });
  assert.equal(cached!.state, "verified");
  assert.equal(restarted.dispatches(), 0);
  await restarted.service.refresh(task.local_task_id);
  assert.equal((await restarted.repo.load()).queue.filter((e) => e.event_type === "device_action_receipt_verification").length, 1);
  const queue = new EventQueue(restarted.repo);
  const batch = (await queue.claimBatch())!;
  await queue.acknowledgeBatch(batch.batch_id, batch.events.map((event) => event.event_id));
  await restarted.service.refresh(task.local_task_id);
  assert.equal((await restarted.repo.load()).queue.length, 0);
  await restarted.service.accept(task.local_task_id, true);
});

test("concurrent UI execution claims the prepared task once", async () => {
  const f = fixture();
  const task = (await f.service.prepare("打开设置"))!;
  await Promise.all([f.service.execute(task.local_task_id, async () => true),
    f.service.execute(task.local_task_id, async () => true)]);
  assert.equal(f.dispatches(), 1);
});

test("cancellation while confirmation is pending prevents subsequent dispatch", async () => {
  const f = fixture();
  const task = (await f.service.prepare("打开设置"))!;
  let allow!: (value: boolean) => void;
  let entered!: () => void;
  const confirming = new Promise<void>((resolve) => { entered = resolve; });
  const run = f.service.execute(task.local_task_id, () => { entered(); return new Promise((resolve) => { allow = resolve; }); });
  await confirming;
  await f.service.cancel(task.local_task_id);
  allow(true);
  await run;
  assert.equal(f.dispatches(), 0);
  const cancelled = (await f.tasks.get(task.local_task_id))!;
  assert.equal(taskViewOf(cancelled).state, "cancelled");
  assert.equal(describeTerminalTask(cancelled)!.title, "任务已取消");
});

test("an interrupted claimed task is never dispatched on restart", async () => {
  const f = fixture();
  const task = (await f.service.prepare("打开设置"))!;
  const interrupted = new DeviceTaskService(f.repo, f.ledger, {
    execute: async () => { throw new Error("process interrupted"); }, getJournal: () => null,
  });
  await assert.rejects(interrupted.execute(task.local_task_id, async () => true), /interrupted/);
  const restarted = fixture(f.store);
  assert.equal(await restarted.service.execute(task.local_task_id, async () => { throw new Error("no new confirmation"); }), null);
  assert.equal(restarted.dispatches(), 0);
  assert.match(taskViewOf((await restarted.tasks.get(task.local_task_id))!).label, /核对|重放/);
  await assert.rejects(restarted.service.accept(task.local_task_id, true));
  await assert.rejects(restarted.service.cancel(task.local_task_id));
});

test("task storage failure before claim prevents any device dispatch", async () => {
  const f = fixture();
  const task = (await f.service.prepare("打开设置"))!;
  f.store.failTaskWrite = true;
  await assert.rejects(f.service.execute(task.local_task_id, async () => true), /storage/);
  assert.equal(f.dispatches(), 0);
  assert.equal(await f.ledger.getProposal(task.device_execution!.proposal.action_id), null);
});

test("receipt survives projection write failure and can be published after restart", async () => {
  const store = new Store();
  const f = fixture(store, { afterDispatch: () => { store.failTaskWrite = true; } });
  const task = (await f.service.prepare("打开设置"))!;
  await assert.rejects(f.service.execute(task.local_task_id, async () => true), /storage/);
  assert.equal(f.dispatches(), 1);
  assert.equal((await f.ledger.getReceipt(task.device_execution!.proposal.action_id))!.state, "verified");
  store.failTaskWrite = false;
  const restarted = fixture(store);
  await restarted.service.refresh(task.local_task_id);
  await restarted.service.accept(task.local_task_id, true);
  assert.equal(restarted.dispatches(), 0);
});

test("unverified action cannot be promoted to task success or retried", async () => {
  const f = fixture(new Store(), { unverified: true });
  const task = (await f.service.prepare("打开设置"))!;
  await f.service.execute(task.local_task_id, async () => true);
  await assert.rejects(f.service.accept(task.local_task_id, true));
  await f.service.execute(task.local_task_id, async () => true);
  assert.equal(f.dispatches(), 1);
  assert.equal((await f.tasks.get(task.local_task_id))!.device_execution!.phase, "needs_review");
});

test("device marker prevents even stale submission statuses from reaching server", async () => {
  const f = fixture();
  const task = (await f.service.prepare("打开设置"))!;
  for (const status of ["LOCAL_ONLY", "SUBMITTING", "NEEDS_RECONCILE"] as const) {
    await f.repo.update((state) => { state.tasks[0].status = status; });
    await f.tasks.recoverInterrupted();
    await f.tasks.flushPending();
    assert.equal(await f.tasks.confirmResubmit(task.local_task_id), false);
  }
  assert.equal(f.serverSubmits(), 0);
});

test("leaving foreground during confirmation does not claim or execute the task", async () => {
  const f = fixture();
  const task = (await f.service.prepare("打开设置"))!;
  await assert.rejects(f.service.execute(task.local_task_id, async () => true, () => false), /回到应用/);
  assert.equal(f.dispatches(), 0);
  assert.equal((await f.tasks.get(task.local_task_id))!.device_execution!.phase, "prepared");
});

test("real sync engine retries metadata only and never submits the device task", async () => {
  const f = fixture();
  const task = (await f.service.prepare("打开设置"))!;
  await f.service.execute(task.local_task_id, async () => true);
  const queue = new EventQueue(f.repo);
  const sent: string[][] = [];
  const sync = new SyncEngine(queue, { syncEvents: async (events) => {
    sent.push(events.map((event) => event.event_id));
    assert.ok(!JSON.stringify(events).includes("local-secret-screen"));
    if (sent.length === 1) throw new Error("offline");
    return { accepted: events.map((event) => event.event_id), duplicates: [], rejected: [] };
  } }, f.tasks);
  assert.equal((await sync.flush(true)).ok, false);
  assert.equal((await sync.flush(true)).ok, true);
  assert.deepEqual(sent[0], sent[1]);
  assert.equal((await queue.unsynced()).length, 0);
  await f.service.refresh(task.local_task_id);
  assert.equal((await queue.unsynced()).length, 0);
  assert.equal(f.serverSubmits(), 0);
  assert.equal(f.dispatches(), 1);
});

test("unknown receipt cannot be forged by corrupting the action ledger", async () => {
  const f = fixture();
  const task = (await f.service.prepare("打开设置"))!;
  await f.store.setItem("device_task_ledger_v1", "corrupt");
  await assert.rejects(f.service.execute(task.local_task_id, async () => true));
  assert.equal(f.dispatches(), 0);
  assert.equal((await f.tasks.get(task.local_task_id))!.device_execution!.phase, "claimed");
});

test("foreground loss after durable claim remains pending without dispatch", async () => {
  const f = fixture();
  const task = (await f.service.prepare("打开设置"))!;
  let checks = 0;
  await assert.rejects(f.service.execute(task.local_task_id, async () => true, () => ++checks === 1), /核对/);
  assert.equal(f.dispatches(), 0);
  assert.equal(await f.service.execute(task.local_task_id, async () => true), null);
  assert.equal(f.dispatches(), 0);
});

test("a proposal changed during confirmation cannot use the old consent", async () => {
  const f = fixture();
  const task = (await f.service.prepare("打开设置"))!;
  await assert.rejects(f.service.execute(task.local_task_id, async () => {
    await f.repo.update((state) => { state.tasks[0].device_execution!.proposal.target = "changed-target"; });
    return true;
  }), /变化/);
  assert.equal(f.dispatches(), 0);
});

test("a truthy consent value is not explicit approval", async () => {
  const f = fixture();
  const task = (await f.service.prepare("打开设置"))!;
  assert.equal(await f.service.execute(task.local_task_id, async () => "true" as any), null);
  assert.equal(f.dispatches(), 0);
});

test("a full critical event queue preserves the device receipt until space is available", async () => {
  const f = fixture();
  const task = (await f.service.prepare("打开设置"))!;
  await f.repo.update((state) => {
    state.queue = [];
    for (let i = 0; i < QUEUE_CAPACITY; i++) enqueueInto(state, {
      event_type: "security_test", payload: {}, severity: "critical",
    });
  });
  await assert.rejects(f.service.execute(task.local_task_id, async () => true), /队列已满/);
  assert.equal(f.dispatches(), 1);
  assert.equal((await f.ledger.getReceipt(task.device_execution!.proposal.action_id))!.state, "verified");
  const queue = new EventQueue(f.repo);
  const batch = (await queue.claimBatch(QUEUE_CAPACITY))!;
  await queue.acknowledgeBatch(batch.batch_id, batch.events.map((event) => event.event_id));
  await f.service.refresh(task.local_task_id);
  assert.equal((await queue.unsynced()).filter((e) => e.event_type === "device_action_receipt_verification").length, 1);
  await f.service.accept(task.local_task_id, true);
  assert.equal(f.dispatches(), 1);
});

test("a reconciled receipt replaces the older unknown projection without another action", async () => {
  const f = fixture(new Store(), { unverified: true });
  const task = (await f.service.prepare("打开设置"))!;
  const prior = (await f.service.execute(task.local_task_id, async () => true))!;
  assert.equal(prior.state, "unverified");
  // A trusted local reconciler supplies new evidence through the existing ledger API.
  await f.ledger.recordReceipt({ ...prior, state: "verified", terminal: true, occurred_at: prior.occurred_at + 1 });
  await f.service.refresh(task.local_task_id);
  const events = (await f.repo.load()).queue.filter((e) => e.event_type === "device_action_receipt_verification");
  assert.deepEqual(events.map((e) => e.payload.state), ["unverified", "verified"]);
  assert.equal(f.dispatches(), 1);
});

test("two repository instances cannot overwrite each other's task updates", async () => {
  const store = new Store();
  const first = new OfflineRepository(store);
  const second = new OfflineRepository(store);
  let entered!: () => void;
  let release!: () => void;
  const reading = new Promise<void>((done) => { entered = done; });
  const wait = new Promise<void>((done) => { release = done; });
  const a = first.update(async (state) => { entered(); await wait; state.next_seq += 1; });
  await reading;
  const b = second.update((state) => { state.next_seq += 1; });
  // Let the second reader start before the first write is released.
  await new Promise<void>((done) => setTimeout(done, 0));
  release();
  await Promise.all([a, b]);
  assert.equal((await first.load()).next_seq, 2);
});
