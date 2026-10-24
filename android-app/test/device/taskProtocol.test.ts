import { test } from "node:test";
import assert from "node:assert/strict";

import { MemoryStore } from "../../src/offline/storage.ts";
import type { KeyValueStore } from "../../src/offline/storage.ts";
import { DeviceBroker } from "../../src/device/broker.ts";
import type { DeviceTransport } from "../../src/device/transport.ts";
import type { DeviceAction, DeviceResult } from "../../src/device/types.ts";
import {
  TaskActionLedger,
  createProposal,
  executeProposal,
  isTerminalJournalState,
  localRetentionFields,
  proposalForUpload,
  proposalToAction,
  receiptForUpload,
} from "../../src/device/taskProtocol.ts";
import type { DeviceActionProposal } from "../../src/device/taskProtocol.ts";

class FakeTransport implements DeviceTransport {
  calls = 0;
  available = true;
  result: DeviceResult = {
    status: "verified",
    evidence: { before: [{ text: "设置" }], after: [{ text: "已设置" }] },
  };

  async isAvailable() {
    return this.available;
  }

  async dispatch(_action: DeviceAction) {
    this.calls += 1;
    return this.result;
  }
}

interface Fixture {
  kv: KeyValueStore;
  transport: FakeTransport;
  broker: DeviceBroker;
  ledger: TaskActionLedger;
  now: () => number;
}

let tick = 0;

function fixture(overrides: Partial<Pick<FakeTransport, "available" | "result">> = {}): Fixture {
  tick = 1_700_000_000_000;
  const kv = new MemoryStore();
  const transport = new FakeTransport();
  if (overrides.available !== undefined) transport.available = overrides.available;
  if (overrides.result !== undefined) transport.result = overrides.result;
  const now = () => tick;
  const broker = new DeviceBroker(transport, { now });
  const ledger = new TaskActionLedger(kv, { now });
  return { kv, transport, broker, ledger, now };
}

function proposal(
  overrides: Partial<Parameters<typeof createProposal>[0]> = {},
): DeviceActionProposal {
  return createProposal({
    task_id: "task-0001",
    kind: "launch_intent",
    target: "com.example.settings",
    ...overrides,
  });
}

test("缺口证明：任务链路引用随动作进入 broker 日志（task→action→effect 可追溯）", async () => {
  const f = fixture();
  const p = proposal();
  const { receipt } = await executeProposal(f.ledger, f.broker, p, { now: f.now });
  const entry = f.broker.getJournal(p.action_id);
  assert.ok(entry, "broker 应记录动作日志");
  assert.equal(entry.task_id, "task-0001");
  assert.equal(entry.run_id, p.run_id);
  assert.equal(entry.effect_id, p.effect_id);
  assert.equal(receipt.task_id, "task-0001");
  assert.equal(receipt.effect_id, p.effect_id);
  assert.equal(receipt.action_id, p.action_id);
});

test("createProposal：引用完整且稳定，run_id 缺省派生自任务，风险按 broker 同表推导", () => {
  const p = proposal({ kind: "send_message", target: "com.example.chat" });
  assert.ok(p.proposal_id && p.action_id && p.effect_id);
  assert.equal(p.run_id, "local:task-0001");
  assert.equal(p.risk, "high");
  const explicit = proposal({ risk: "low", run_id: "run_9" });
  assert.equal(explicit.risk, "low");
  assert.equal(explicit.run_id, "run_9");
  const medium = proposal({ kind: "click_text", target: "设置" });
  assert.equal(medium.risk, "medium");
  assert.throws(() => createProposal({ task_id: "", kind: "launch_intent", target: "x" }));
  assert.throws(() => createProposal({ task_id: "t", kind: "launch_intent", target: " " }));
});

test("proposalToAction：预期观察映射 expect_text，引用随动作下发", () => {
  const p = proposal({
    args: { intent_action: "android.settings.WIRELESS_SETTINGS" },
    expected_observation: { expect_text: "WLAN" },
  });
  const action = proposalToAction(p);
  assert.equal(action.action_id, p.action_id);
  assert.equal(action.task_id, p.task_id);
  assert.equal(action.effect_id, p.effect_id);
  assert.equal(action.args?.expect_text, "WLAN");
  assert.equal(action.args?.intent_action, "android.settings.WIRELESS_SETTINGS");
});

test("执行成功：回执终态 verified、证据与时间戳齐备，任务链可查", async () => {
  const f = fixture();
  const p = proposal();
  const { receipt, dispatched } = await executeProposal(f.ledger, f.broker, p, { now: f.now });
  assert.equal(dispatched, true);
  assert.equal(receipt.state, "verified");
  assert.equal(receipt.terminal, true);
  assert.equal(receipt.replayed, false);
  assert.ok(receipt.evidence && Object.keys(receipt.evidence).length > 0);
  assert.equal(receipt.occurred_at, tick);
  const refs = await f.ledger.listForTask("task-0001");
  assert.equal(refs.length, 1);
  assert.equal(refs[0].proposal.action_id, p.action_id);
  assert.equal(refs[0].receipt?.state, "verified");
});

test("同一 action_id 重放：返回原回执，不重复下发，原始证据保留", async () => {
  const f = fixture();
  const p = proposal();
  const first = await executeProposal(f.ledger, f.broker, p, { now: f.now });
  assert.equal(f.transport.calls, 1);
  const second = await executeProposal(f.ledger, f.broker, p, { now: f.now });
  assert.equal(f.transport.calls, 1, "幂等重放不得重复下发");
  assert.equal(second.dispatched, false);
  assert.equal(second.receipt.replayed, true);
  assert.equal(second.receipt.state, first.receipt.state);
  assert.deepEqual(second.receipt.evidence, first.receipt.evidence);
});

test("未知副作用（unverified）不是终态：不自动重放，需人工对账", async () => {
  const f = fixture({
    result: { status: "unverified", message: "页面未变化", evidence: {} },
  });
  const p = proposal();
  const first = await executeProposal(f.ledger, f.broker, p, { now: f.now });
  assert.equal(first.receipt.state, "unverified");
  assert.equal(first.receipt.terminal, false);
  assert.equal(isTerminalJournalState("unverified"), false);
  const second = await executeProposal(f.ledger, f.broker, p, { now: f.now });
  assert.equal(f.transport.calls, 1, "已下发未验证动作禁止自动重放");
  assert.equal(second.dispatched, false);
  assert.equal(second.receipt.replayed, true);
});

test("高风险无确认：终态 denied；重放不重复询问、不下发", async () => {
  const f = fixture();
  const p = proposal({ kind: "send_message", target: "com.example.chat" });
  const { receipt } = await executeProposal(f.ledger, f.broker, p, { now: f.now });
  assert.equal(receipt.state, "denied");
  assert.equal(receipt.terminal, true);
  assert.equal(f.transport.calls, 0);
  const again = await executeProposal(f.ledger, f.broker, p, { now: f.now });
  assert.equal(again.receipt.replayed, true);
  assert.equal(f.transport.calls, 0);
});

test("无障碍未授权：终态 unavailable，重试需新提议（新 action_id）", async () => {
  const f = fixture({ available: false });
  const p = proposal();
  const { receipt } = await executeProposal(f.ledger, f.broker, p, { now: f.now });
  assert.equal(receipt.state, "unavailable");
  assert.equal(receipt.terminal, true);
  assert.ok(receipt.message?.includes("无障碍"));
  const again = await executeProposal(f.ledger, f.broker, p, { now: f.now });
  assert.equal(again.receipt.replayed, true);
  assert.equal(f.transport.calls, 0);
  // 授权后重试 = 新提议新 action_id，可正常执行
  f.transport.available = true;
  const retry = proposal();
  const second = await executeProposal(f.ledger, f.broker, retry, { now: f.now });
  assert.equal(second.receipt.state, "verified");
  assert.equal(f.transport.calls, 1);
});

test("失败动作不自动重放：同一 action_id 返回原失败回执", async () => {
  const f = fixture({ result: { status: "failed", message: "节点未找到" } });
  const p = proposal();
  const { receipt } = await executeProposal(f.ledger, f.broker, p, { now: f.now });
  assert.equal(receipt.state, "failed");
  const again = await executeProposal(f.ledger, f.broker, p, { now: f.now });
  assert.equal(again.dispatched, false);
  assert.equal(again.receipt.state, "failed");
  assert.equal(f.transport.calls, 1);
});

test("台账持久化：重启（新实例）后提议、回执与任务链完整", async () => {
  const f = fixture();
  const p = proposal();
  await executeProposal(f.ledger, f.broker, p, { now: f.now });
  const revived = new TaskActionLedger(f.kv, { now: f.now });
  const refs = await revived.listForTask("task-0001");
  assert.equal(refs.length, 1);
  assert.equal(refs[0].receipt?.state, "verified");
  const replay = await executeProposal(revived, f.broker, p, { now: f.now });
  assert.equal(replay.dispatched, false, "重启后同一 action_id 仍不重复下发");
  assert.equal(replay.receipt.replayed, true);
});

test("台账容量上界：超出 limit 淘汰最旧条目", async () => {
  const f = fixture();
  const small = new TaskActionLedger(f.kv, { now: f.now, limit: 2 });
  const a = proposal({ task_id: "t-a" });
  const b = proposal({ task_id: "t-b" });
  const c = proposal({ task_id: "t-c" });
  await executeProposal(small, f.broker, a, { now: f.now });
  await executeProposal(small, f.broker, b, { now: f.now });
  await executeProposal(small, f.broker, c, { now: f.now });
  const state = await small.loadState();
  assert.equal(Object.keys(state.proposals).length, 2);
  assert.equal(state.proposals[a.action_id], undefined);
  assert.ok(state.proposals[c.action_id]);
});

test("上传投影：回执只含引用与状态，提议脱敏文本目标，本地保留字段列明", () => {
  const p = proposal({
    kind: "click_text",
    target: "发送",
    args: { message_text: "秘密内容" },
  });
  const upload = proposalForUpload(p);
  assert.equal(upload.target_kind, "text");
  assert.equal(upload.target, "<local:text>");
  assert.equal((upload as Record<string, unknown>).args, undefined);
  const pkg = proposalForUpload(proposal());
  assert.equal(pkg.target_kind, "package");
  assert.equal(pkg.target, "com.example.settings");
  const receipt = receiptForUpload({
    effect_id: p.effect_id,
    action_id: p.action_id,
    proposal_id: p.proposal_id,
    task_id: p.task_id,
    run_id: p.run_id,
    kind: p.kind,
    risk: p.risk,
    state: "verified",
    terminal: true,
    replayed: false,
    message: "已验证",
    evidence: { before: [1], after: [2] },
    occurred_at: 1,
    recorded_at: 2,
  });
  assert.equal(receipt.has_evidence, true);
  assert.equal((receipt as Record<string, unknown>).evidence, undefined);
  assert.equal((receipt as Record<string, unknown>).message, undefined);
  const retention = localRetentionFields(p);
  for (const field of ["args", "message", "evidence", "target"]) {
    assert.ok(retention.includes(field), `本地保留字段缺 ${field}`);
  }
});
