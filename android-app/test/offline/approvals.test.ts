import { test } from "node:test";
import assert from "node:assert/strict";
import type { ApprovalInfo } from "../../src/api.ts";
import { ApprovalSession, approvalStatus, fullBinding } from "../../src/features/approvals/state.ts";

function fixture() {
  let detail: ApprovalInfo = { approval_id: "a", task_id: "t", op: "send", status: "pending",
    binding_json: JSON.stringify({ args: { target: "A", body: "full message" }, scope: "react:1", version: "1" }),
    action_digest: "hash", action_summary: "send to A", expires_at: Date.now() / 1000 + 60 };
  const calls: boolean[] = [];
  const transport = {
    getApproval: async () => ({ ...detail }),
    decideApproval: async (_id: string, approved: boolean) => {
      calls.push(approved); detail.status = approved ? "used" : "denied";
      return { accepted: true, task_id: "t", task_status: "RUNNING" };
    },
    getTask: async () => ({ task_id: "t", task_status: "RUNNING" } as any),
  };
  const session = new ApprovalSession(transport, "a", "t");
  session.configure("secret-admin", true);
  return { session, transport, detail, calls };
}

test("shows full binding, accepts once, and does not equate acceptance with task success", async () => {
  const { session, calls } = fixture();
  assert.equal(session.canDecide(), false);
  await session.refresh();
  assert.match(fullBinding(session.state.detail!), /full message/);
  assert.equal(session.canDecide(), true);
  await Promise.all([session.decide(true), session.decide(true)]);
  assert.deepEqual(calls, [true]);
  assert.equal(session.state.detail?.status, "used");
  assert.equal(session.state.taskStatus, "RUNNING");
  assert.match(session.state.notice, /仍需刷新/);
  assert.equal(JSON.stringify(session.state).includes("secret-admin"), false);
});

test("reject and per-approval state are independent", async () => {
  const a = fixture(), b = fixture();
  await a.session.refresh(); await b.session.refresh();
  await a.session.decide(false);
  assert.deepEqual(a.calls, [false]);
  assert.equal(a.session.state.detail?.status, "denied");
  assert.equal(b.session.canDecide(), true);
});

test("expiration and offline mode disable decisions while retaining details", async () => {
  const { session, detail, calls } = fixture();
  await session.refresh();
  detail.expires_at = Date.now() / 1000 - 1;
  await session.refresh();
  assert.match(approvalStatus(session.state.detail), /到期/);
  await session.decide(true);
  session.configure("secret-admin", false);
  assert.equal(session.canDecide(), false);
  assert.ok(session.state.detail);
  assert.deepEqual(calls, []);
});

test("status or binding changes on re-read require review before decision", async () => {
  const { session, detail, calls } = fixture();
  await session.refresh();
  detail.action_digest = "changed";
  await session.decide(true);
  assert.deepEqual(calls, []);
  assert.match(session.state.notice, /已变化/);
});

for (const status of [401, 403, 404, 409, 500]) {
  test(`HTTP ${status} errors never echo a credential or server error body`, async () => {
    const { session, transport } = fixture();
    await session.refresh();
    transport.decideApproval = async () => { throw Object.assign(new Error("secret-admin"), { status }); };
    await session.decide(true);
    assert.ok(session.state.error);
    assert.equal(session.canDecide(), false);
    assert.equal(JSON.stringify(session.state).includes("secret-admin"), false);
  });
}

test("an ambiguous POST is checked by the same approval id before any retry", async () => {
  const { session, transport, detail, calls } = fixture();
  await session.refresh();
  transport.decideApproval = async () => { calls.push(true); detail.status = "used"; throw Error("lost reply"); };
  await session.decide(true);
  await session.decide(true);
  assert.equal(calls.length, 1);
  await session.refresh();
  assert.equal(session.state.detail?.status, "used");
  assert.equal(session.canDecide(), false);
});

test("responses from a previous credential or disposed page cannot enable a button", async () => {
  const { session, transport, detail } = fixture();
  let resolve!: (value: ApprovalInfo) => void;
  transport.getApproval = () => new Promise(r => { resolve = r; });
  const reading = session.refresh();
  session.configure("new-admin", true);
  resolve(detail); await reading;
  assert.equal(session.state.detail, undefined);
  assert.equal(session.canDecide(), false);
  const again = session.refresh(); session.dispose(); resolve(detail); await again;
  assert.equal(session.state.detail, undefined);
});

test("a response for another task cannot be approved", async () => {
  const { session, detail } = fixture(); detail.task_id = "other";
  await session.refresh();
  assert.equal(session.canDecide(), false);
  assert.ok(session.state.error);
});
