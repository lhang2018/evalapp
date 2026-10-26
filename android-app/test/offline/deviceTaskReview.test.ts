import { test } from "node:test";
import assert from "node:assert/strict";
import { DeviceTaskService } from "../../src/features/pipeline/deviceTasks.ts";
import { MemoryStore, OfflineRepository, STATE_KEY } from "../../src/offline/storage.ts";
import { TaskActionLedger, createProposal } from "../../src/device/taskProtocol.ts";
import type { ActionReceipt, DeviceActionProposal } from "../../src/device/taskProtocol.ts";
import { DeviceBroker } from "../../src/device/broker.ts";
import type { LocalTask } from "../../src/offline/types.ts";

class Store extends MemoryStore {
  onTaskWrite: (() => void) | null = null;
  async setItem(key: string, value: string) {
    await super.setItem(key, value);
    if (key === STATE_KEY) this.onTaskWrite?.();
  }
}
function fixture() {
  const kv = new Store(), repo = new OfflineRepository(kv), ledger = new TaskActionLedger(kv);
  let calls = 0;
  const broker = new DeviceBroker({ isAvailable: async () => true,
    dispatch: async () => { calls++; return { status: "verified", evidence: { fixture: true } }; } });
  return { kv, repo, ledger, service: new DeviceTaskService(repo, ledger, broker), calls: () => calls };
}
function receipt(p: DeviceActionProposal): ActionReceipt {
  return { action_id: p.action_id, effect_id: p.effect_id, proposal_id: p.proposal_id,
    task_id: p.task_id, run_id: p.run_id, kind: p.kind, risk: p.risk,
    state: "verified", terminal: true, replayed: false, occurred_at: 1, recorded_at: 2 };
}

const edits: Array<[string, (task: LocalTask) => void]> = [
  ["target", (t) => { t.device_execution!.proposal.target = "com.other.app"; }],
  ["kind", (t) => { t.device_execution!.proposal.kind = "click_text"; }],
  ["args", (t) => { t.device_execution!.proposal.args = { recipient: "other" }; }],
  ["expectation", (t) => { t.device_execution!.proposal.expected_observation = { expect_text: "other" }; }],
  ["risk", (t) => { t.device_execution!.proposal.risk = "medium"; }],
  ["goal", (t) => { t.goal = "打开设置然后点击支付"; }],
];
for (const [name, edit] of edits) {
  test(`persisted ${name} outside the displayed settings scope is refused before consent`, async () => {
    const f = fixture(), task = (await f.service.prepare("打开设置"))!;
    await f.repo.update((state) => edit(state.tasks[0]));
    let confirmations = 0;
    await assert.rejects(f.service.execute(task.local_task_id, async () => { confirmations++; return true; }));
    assert.equal(confirmations, 0);
    assert.equal(f.calls(), 0);
  });
}

test("receipt downgrade removes the stale awaiting-acceptance task state", async () => {
  const f = fixture(), task = (await f.service.prepare("打开设置"))!;
  const verified = (await f.service.execute(task.local_task_id, async () => true))!;
  await f.ledger.recordReceipt({ ...verified, state: "unverified", terminal: false });
  await f.service.refresh(task.local_task_id);
  assert.equal((await f.repo.load()).tasks[0].device_execution!.phase, "needs_review");
  await assert.rejects(f.service.accept(task.local_task_id, true));
});

test("acceptance cannot use a receipt revoked while publishing its projection", { timeout: 3000 }, async () => {
  const f = fixture(), task = (await f.service.prepare("打开设置"))!;
  const verified = (await f.service.execute(task.local_task_id, async () => true))!;
  let revocation: Promise<void> | null = null;
  f.kv.onTaskWrite = () => {
    f.kv.onTaskWrite = null;
    revocation = f.ledger.recordReceipt({ ...verified, state: "unverified", terminal: false });
  };
  let accepted = false;
  try { await f.service.accept(task.local_task_id, true); accepted = true; } catch { /* expected */ }
  if (revocation) await revocation;
  assert.equal(accepted, false);
  assert.notEqual((await f.repo.load()).tasks[0].status, "DONE");
});

for (const [name, change] of [
  ["kind", { kind: "payment" }], ["risk", { risk: "high" }],
  ["terminal", { terminal: false }], ["unknown state", { state: "future", terminal: false }],
  ["replay flag", { replayed: "true" }], ["timestamp", { occurred_at: NaN }],
] as const) {
  test(`invalid receipt ${name} is rejected before changing durable history`, async () => {
    const f = fixture(), p = createProposal({ task_id: "t", kind: "launch_intent", target: "com.android.settings" });
    await f.ledger.recordProposal(p);
    const original = await f.kv.getItem("device_task_ledger_v1");
    await assert.rejects(f.ledger.recordReceipt({ ...receipt(p), ...change } as any));
    assert.equal(await f.kv.getItem("device_task_ledger_v1"), original);
    assert.ok(await f.ledger.getProposal(p.action_id));
  });
}

test("orphan recovery receipt cannot poison the entire action ledger", async () => {
  const f = fixture(), p = createProposal({ task_id: "t", kind: "launch_intent", target: "com.android.settings" });
  await assert.rejects(f.ledger.recordRecoveryReceipt({ ...receipt(p), state: "unverified", terminal: false }));
  assert.equal(await f.kv.getItem("device_task_ledger_v1"), null);
});

test("recovery cannot borrow a cached receipt from another task", async () => {
  const f = fixture(), p = createProposal({ task_id: "t", kind: "launch_intent", target: "com.android.settings" });
  await f.ledger.recordProposal(p);
  await f.ledger.recordReceipt(receipt(p));
  await assert.rejects(f.ledger.recordRecoveryReceipt({ ...receipt(p), task_id: "other-task" }));
  assert.equal((await f.ledger.getReceipt(p.action_id))!.task_id, "t");
});

test("dependent task write holds off a second ledger instance until it completes", { timeout: 3000 }, async () => {
  const f = fixture(), p = createProposal({ task_id: "t", kind: "launch_intent", target: "com.android.settings" });
  await f.ledger.recordProposal(p);
  await f.ledger.recordReceipt(receipt(p));
  let entered!: () => void;
  let release!: () => void;
  const ready = new Promise<void>((resolve) => { entered = resolve; });
  const wait = new Promise<void>((resolve) => { release = resolve; });
  const reading = f.ledger.withReceipt(p.action_id, async (current) => {
    await f.repo.update(async () => { entered(); await wait; });
    return current!.state;
  });
  await ready;
  let updated = false;
  const writing = new TaskActionLedger(f.kv).recordReceipt({ ...receipt(p), state: "unverified", terminal: false })
    .then(() => { updated = true; });
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
  assert.equal(updated, false);
  release();
  assert.equal(await reading, "verified");
  await writing;
  assert.equal((await f.ledger.getReceipt(p.action_id))!.state, "unverified");
});

test("a failed dependent write releases the receipt gate", async () => {
  const f = fixture(), p = createProposal({ task_id: "t", kind: "launch_intent", target: "com.android.settings" });
  await f.ledger.recordProposal(p);
  await assert.rejects(f.ledger.withReceipt(p.action_id, async () => { throw new Error("dependent write failed"); }));
  await new TaskActionLedger(f.kv).recordReceipt(receipt(p));
  assert.equal((await f.ledger.getReceipt(p.action_id))!.state, "verified");
});

test("user acceptance retains the exact receipt seen even if later evidence changes", async () => {
  const f = fixture(), task = (await f.service.prepare("打开设置"))!;
  const seen = (await f.service.execute(task.local_task_id, async () => true))!;
  await f.service.accept(task.local_task_id, true);
  const accepted = (await f.repo.load()).tasks[0].device_execution!.accepted_receipt;
  assert.equal(JSON.parse(accepted!).occurred_at, seen.occurred_at);
  assert.equal(JSON.parse(accepted!).state, "verified");
  await f.ledger.recordReceipt({ ...seen, state: "unverified", terminal: false });
  await f.service.refresh(task.local_task_id);
  assert.equal((await f.repo.load()).tasks[0].device_execution!.accepted_receipt, accepted);
  const event = (await f.repo.load()).queue.find((e) => e.event_type === "device_task_accepted")!;
  assert.match(String(event.payload.receipt_sha256), /^[a-f0-9]{64}$/);
});
