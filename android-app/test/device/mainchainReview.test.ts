import { test } from "node:test";
import assert from "node:assert/strict";
import { DeviceBroker } from "../../src/device/broker.ts";
import { NativeDeviceTransport } from "../../src/device/nativeTransport.ts";
import { createProposal, executeProposal, TaskActionLedger } from "../../src/device/taskProtocol.ts";
import { MemoryStore } from "../../src/offline/storage.ts";
import type { DeviceAction } from "../../src/device/types.ts";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

test("confirmation callback cannot rewrite the action identity or nested parameters", async () => {
  const sent: DeviceAction[] = [];
  const broker = new DeviceBroker({ isAvailable: async () => true,
    dispatch: async (action) => { sent.push(action); return { status: "unverified" }; } }, {
    confirm: (candidate) => {
      candidate.target = "different-recipient";
      candidate.task_id = "different-task";
      (candidate.args!.payload as { text: string }).text = "different-body";
      return true;
    },
  });
  const proposal = createProposal({ task_id: "task", run_id: "run", kind: "send_message",
    target: "confirmed-recipient", args: { payload: { text: "confirmed-body" } } });
  const result = await executeProposal(new TaskActionLedger(new MemoryStore()), broker, proposal);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].target, "confirmed-recipient");
  assert.equal(sent[0].task_id, "task");
  assert.equal((sent[0].args!.payload as { text: string }).text, "confirmed-body");
  assert.equal(result.receipt.task_id, proposal.task_id);
});

for (const response of ["false", "true", 1, {}]) {
  test(`non-boolean confirmation ${JSON.stringify(response)} cannot authorize execution`, async () => {
    let calls = 0;
    const broker = new DeviceBroker({ isAvailable: async () => true,
      dispatch: async () => { calls++; return { status: "verified", evidence: { fixture: true } }; } },
      { confirm: async () => response as any });
    const result = await broker.execute({ action_id: "high", kind: "payment", target: "recipient" });
    assert.equal(result.status, "denied");
    assert.equal(calls, 0);
  });
}

for (const waitingOn of ["availability", "confirmation"]) {
  test(`safety stop prevents dispatch after waiting on ${waitingOn}`, async () => {
    const gate = deferred<boolean>();
    const entered = deferred<void>();
    let availabilityCalls = 0;
    const sent: string[] = [];
    const broker = new DeviceBroker({ isAvailable: async () => {
      availabilityCalls++;
      if (waitingOn === "availability" && availabilityCalls === 1) {
        entered.resolve(); return gate.promise;
      }
      return true;
    }, dispatch: async (action) => { sent.push(action.action_id); return { status: "unverified" }; } }, {
      maxConsecutiveUnverified: 1,
      confirm: async () => { entered.resolve(); return gate.promise; },
    });
    const waiting = broker.execute({ action_id: "waiting", kind: "launch_intent",
      target: "com.android.settings", risk: waitingOn === "confirmation" ? "high" : "low" });
    await entered.promise;
    await broker.execute({ action_id: "trigger-stop", kind: "launch_intent", target: "com.android.settings" });
    assert.equal(broker.isStopped(), true);
    gate.resolve(true);
    assert.equal((await waiting).status, "denied");
    assert.deepEqual(sent, ["trigger-stop"]);
    assert.equal(broker.getJournal("waiting")?.state, "denied");
  });
}

for (const ok of ["false", "true", 1, {}]) {
  test(`native truthy ok ${JSON.stringify(ok)} is not verified success`, async () => {
    const transport = new NativeDeviceTransport({ isServiceEnabled: async () => true,
      launchIntent: async () => ({ ok: ok as any, evidence: { fixture: true } }),
      performClick: async () => ({ ok: false }) });
    const result = await transport.dispatch({ action_id: "native", kind: "launch_intent", target: "com.android.settings" });
    assert.equal(result.status, "unverified");
  });
}

test("native service availability must be literal true before dispatch", async () => {
  let calls = 0;
  const broker = new DeviceBroker(new NativeDeviceTransport({
    isServiceEnabled: async () => "false" as any,
    launchIntent: async () => { calls++; return { ok: true }; },
    performClick: async () => ({ ok: false }),
  }));
  assert.equal((await broker.execute({ action_id: "native", kind: "launch_intent", target: "com.android.settings" })).status, "unavailable");
  assert.equal(calls, 0);
});
