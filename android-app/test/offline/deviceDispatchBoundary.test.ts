import assert from "node:assert/strict";
import { test } from "node:test";
import { DeviceBroker } from "../../src/device/broker.ts";
import { createProposal, executeProposal, TaskActionLedger } from "../../src/device/taskProtocol.ts";
import { DeviceTaskService } from "../../src/features/pipeline/deviceTasks.ts";
import { MemoryStore, OfflineRepository } from "../../src/offline/storage.ts";

function gate<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

test("leaving foreground while availability waits prevents the actual settings dispatch", async () => {
  const kv = new MemoryStore(), repo = new OfflineRepository(kv), ledger = new TaskActionLedger(kv);
  const entered = gate<void>(), available = gate<boolean>();
  let foreground = true, calls = 0;
  const broker = new DeviceBroker({ isAvailable: async () => { entered.resolve(); return available.promise; },
    dispatch: async () => { calls++; return { status: "verified", evidence: { fixture: true } }; } });
  const service = new DeviceTaskService(repo, ledger, broker);
  const task = (await service.prepare("打开设置"))!;
  const running = service.execute(task.local_task_id, async () => true, () => foreground);
  await entered.promise;
  foreground = false;
  available.resolve(true);
  const receipt = await running;
  assert.equal(calls, 0);
  assert.equal(receipt!.state, "denied");
  assert.equal((await repo.load()).tasks[0].device_execution!.phase, "needs_review");
  await service.refresh(task.local_task_id);
  assert.equal(calls, 0);
});

test("guard is rechecked after high risk confirmation rather than before the await", async () => {
  const entered = gate<void>(), consent = gate<boolean>();
  let foreground = true, calls = 0;
  const broker = new DeviceBroker({ isAvailable: async () => true,
    dispatch: async () => { calls++; return { status: "unverified" }; } },
    { confirm: async () => { entered.resolve(); return consent.promise; } });
  const running = broker.execute({ action_id: "guard-after-confirm", kind: "send_message", target: "synthetic-recipient" },
    { canDispatch: () => foreground });
  await entered.promise;
  foreground = false;
  consent.resolve(true);
  assert.equal((await running).status, "denied");
  assert.equal(calls, 0);
});

for (const check of [() => "true", () => 1, () => { throw new Error("guard unavailable"); }]) {
  test(`dispatch gate needs literal true and contains errors: ${String(check)}`, async () => {
    let calls = 0;
    const broker = new DeviceBroker({ isAvailable: async () => true,
      dispatch: async () => { calls++; return { status: "unverified" }; } });
    const result = await broker.execute({ action_id: "strict-guard", kind: "launch_intent", target: "com.android.settings" },
      { canDispatch: check as unknown as () => boolean });
    assert.equal(result.status, "denied");
    assert.equal(calls, 0);
  });
}

for (const mode of ["unavailable", "denied"] as const) {
  test(`pre-dispatch ${mode} is not reported as dispatched by the proposal executor`, async () => {
    let calls = 0;
    const broker = new DeviceBroker({ isAvailable: async () => mode !== "unavailable",
      dispatch: async () => { calls++; return { status: "unverified" }; } }, { confirm: async () => false });
    const result = await executeProposal(new TaskActionLedger(new MemoryStore()), broker,
      createProposal({ task_id: "task", kind: "send_message", target: "synthetic-recipient" }));
    assert.equal(result.dispatched, false);
    assert.equal(calls, 0);
  });
}

test("a legacy broker cannot silently ignore a requested dispatch guard", async () => {
  let calls = 0;
  const ledger = new TaskActionLedger(new MemoryStore());
  const proposal = createProposal({ task_id: "task", kind: "launch_intent", target: "com.android.settings" });
  await assert.rejects(executeProposal(ledger, {
    execute: async () => { calls++; return { status: "unverified" }; }, getJournal: () => null,
  }, proposal, { canDispatch: () => false }), /guard|条件/);
  assert.equal(calls, 0);
  assert.equal(await ledger.getProposal(proposal.action_id), null);
});

test("a transport attempt remains dispatched even if the transport returns denied", async () => {
  let calls = 0;
  const broker = new DeviceBroker({ isAvailable: async () => true,
    dispatch: async () => { calls++; return { status: "denied" }; } });
  const result = await executeProposal(new TaskActionLedger(new MemoryStore()), broker,
    createProposal({ task_id: "task", kind: "launch_intent", target: "com.android.settings" }),
    { canDispatch: () => true });
  assert.equal(calls, 1);
  assert.equal(result.dispatched, true);
});

test("legacy adapters without a dispatch fact report unknown rather than false", async () => {
  const result = await executeProposal(new TaskActionLedger(new MemoryStore()), {
    execute: async () => ({ status: "unverified" }), getJournal: () => null,
  }, createProposal({ task_id: "task", kind: "launch_intent", target: "com.android.settings" }));
  assert.equal(result.dispatched, null);
  assert.equal(result.receipt.state, "unverified");
});
