import { test } from "node:test";
import assert from "node:assert/strict";
import { MemoryStore } from "../../src/offline/storage.ts";
import { DeviceBroker } from "../../src/device/broker.ts";
import { TaskActionLedger, createProposal, executeProposal, proposalForUpload,
  receiptFromJournal } from "../../src/device/taskProtocol.ts";

const make = (extra = {}) => createProposal({ task_id: "review-task", kind: "launch_intent", target: "com.android.settings", ...extra });
function broker(counter: { calls: number }) {
  return new DeviceBroker({ isAvailable: async () => true,
    dispatch: async () => { counter.calls++; return { status: "verified", evidence: { synthetic: true } }; } });
}

test("two ledger instances sharing durable storage claim an action only once", async () => {
  const kv = new MemoryStore(), calls = { calls: 0 }, p = make();
  await Promise.all([executeProposal(new TaskActionLedger(kv), broker(calls), p),
    executeProposal(new TaskActionLedger(kv), broker(calls), p)]);
  assert.equal(calls.calls, 1);
});

for (const field of ["task_id", "run_id", "effect_id", "target"]) {
  test(`cached action id cannot be reused with a different ${field}`, async () => {
    const kv = new MemoryStore(), ledger = new TaskActionLedger(kv), calls = { calls: 0 }, p = make();
    await executeProposal(ledger, broker(calls), p);
    await assert.rejects(executeProposal(ledger, broker(calls), { ...p, [field]: "different" }));
    assert.equal(calls.calls, 1);
    assert.equal((await ledger.getProposal(p.action_id))?.task_id, p.task_id);
  });
}

test("proposal owns nested input snapshots", () => {
  const args = { nested: { text: "original" } }, observation = { expect_text: "original" };
  const p = make({ args, expected_observation: observation });
  args.nested.text = "changed"; observation.expect_text = "changed";
  assert.equal((p.args?.nested as any).text, "original");
  assert.equal(p.expected_observation?.expect_text, "original");
});

test("retention does not make an already dispatched action executable again after restart", async () => {
  const kv = new MemoryStore(), ledger = new TaskActionLedger(kv, { limit: 1 }), calls = { calls: 0 }, p = make();
  await executeProposal(ledger, broker(calls), p);
  await executeProposal(ledger, broker(calls), make());
  await executeProposal(new TaskActionLedger(kv, { limit: 1 }), broker(calls), p);
  assert.equal(calls.calls, 2);
});

test("pending proposal is not evicted to permit replay", async () => {
  const kv = new MemoryStore(), ledger = new TaskActionLedger(kv, { limit: 1 }), p = make();
  await ledger.recordProposal(p);
  await assert.rejects(ledger.recordProposal(make()));
  assert.ok(await ledger.getProposal(p.action_id));
});

test("invalid persisted ledger is rejected rather than becoming an empty execution history", async () => {
  const kv = new MemoryStore(); await kv.setItem("device_task_ledger_v1", "corrupt");
  const calls = { calls: 0 };
  await assert.rejects(executeProposal(new TaskActionLedger(kv), broker(calls), make()));
  assert.equal(calls.calls, 0);
});

test("missing broker journal after dispatch remains unknown", async () => {
  const kv = new MemoryStore();
  const result = await executeProposal(new TaskActionLedger(kv), {
    execute: async () => ({ status: "verified", evidence: { sent: true } }), getJournal: () => null,
  }, make());
  assert.equal(result.receipt.state, "unverified"); assert.equal(result.receipt.terminal, false);
});

test("journal receipt cannot refer to another action", () => {
  const p = make();
  assert.throws(() => receiptFromJournal(p, { action_id: "other", kind: p.kind, risk: p.risk,
    state: "verified", created_at: 1, updated_at: 2 }, { replayed: false, recordedAt: 3 }));
});

test("upload never contains expected screen text or recipient targets", () => {
  const p = make({ kind: "send_message", target: "private-recipient", expected_observation: { expect_text: "private-screen-text" } });
  const encoded = JSON.stringify(proposalForUpload(p));
  assert.ok(!encoded.includes("private-recipient") && !encoded.includes("private-screen-text"));
});

test("explicit low risk cannot downgrade a known high risk device operation", async () => {
  const p = make({ kind: "send_message", risk: "low" }); assert.equal(p.risk, "high");
  const calls = { calls: 0 }, b = broker(calls);
  const result = await b.execute({ action_id: "direct", kind: "payment", target: "payment-target", risk: "low" });
  assert.equal(result.status, "denied"); assert.equal(calls.calls, 0);
});

test("raw broker concurrent duplicates cannot both dispatch", async () => {
  const calls = { calls: 0 }, b = broker(calls);
  const action = { action_id: "raw", kind: "launch_intent", target: "com.android.settings" };
  await Promise.all([b.execute(action), b.execute(action)]);
  assert.equal(calls.calls, 1);
});

test("raw broker never returns another task's cached action receipt", async () => {
  const calls = { calls: 0 }, b = broker(calls);
  await b.execute({ action_id: "same", kind: "launch_intent", target: "com.android.settings", task_id: "first" });
  const result = await b.execute({ action_id: "same", kind: "launch_intent", target: "com.android.settings", task_id: "second" });
  assert.equal(result.status, "denied"); assert.equal(calls.calls, 1);
});
