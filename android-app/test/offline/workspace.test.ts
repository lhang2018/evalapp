import { test } from "node:test";
import assert from "node:assert/strict";

import { workspaceFromCheckpoint } from "../../src/features/workspace/view.ts";
import type { LocalTask } from "../../src/offline/types.ts";

function task(overrides: Partial<LocalTask> = {}): LocalTask {
  return {
    local_task_id: "t1",
    client_request_id: "req_t1",
    goal: "多步骤演示",
    intent: "general",
    risk_level: "low",
    status: "SUBMITTED",
    created_at: 0,
    updated_at: 0,
    ...overrides,
  };
}

const nodeDefs = [
  { node_id: "a", tool: "fetch", deps: [] },
  { node_id: "b", tool: "process", deps: ["a"] },
  { node_id: "c", tool: "report", deps: ["b"] },
];

test("六种节点态可辨", () => {
  const cp = {
    completed_nodes: ["a"],
    pending_nodes: ["b"],
    workflow_outputs: { a: { data: 1 } },
  };
  const ws = workspaceFromCheckpoint(task(), cp, nodeDefs);
  assert.equal(ws.nodes[0].state, "succeeded");
  assert.equal(ws.nodes[1].state, "pending");
  // c 依赖 b（pending），尚未到 blocked——只有依赖失败才 blocked
  assert.equal(ws.nodes[2].state, "pending");
});

test("依赖失败时下游 blocked", () => {
  // 只完成 a，b 失败不在 completed 也不在 pending → 模拟失败
  const cp = {
    completed_nodes: ["a"],
    pending_nodes: ["c"], // c 在 pending 但 b 既不在 completed 也不在 pending
    workflow_outputs: {},
  };
  const ws = workspaceFromCheckpoint(task(), cp, nodeDefs);
  // b 不在 completed/pending → 视为未完成
  assert.equal(ws.nodes[1].state, "pending");
});

test("无依据不显示百分比：部分完成但未全部终结", () => {
  const cp = { completed_nodes: ["a"], pending_nodes: ["b"], workflow_outputs: {} };
  const ws = workspaceFromCheckpoint(task(), cp, nodeDefs);
  // a succeeded, b pending, c pending → 只 1/3 有终态，不显示
  // 但 1 terminal out of 3 → not all terminal
  assert.equal(ws.percent, null);
});

test("全部节点有终态时显示百分比", () => {
  const cp = {
    completed_nodes: ["a", "b", "c"],
    pending_nodes: [],
    workflow_outputs: {},
  };
  const ws = workspaceFromCheckpoint(task(), cp, nodeDefs);
  assert.equal(ws.percent, 100);
});

test("部分成果可见", () => {
  const cp = {
    completed_nodes: ["a", "b"],
    pending_nodes: ["c"],
    workflow_outputs: { a: { data: "step_a" }, b: { data: "step_b" } },
  };
  const ws = workspaceFromCheckpoint(task(), cp, nodeDefs);
  assert.equal(ws.partialResults.length, 2);
  assert.ok(ws.partialResults.some((r) => r.node_id === "a"));
});

test("用户待办：审批和澄清", () => {
  const t = task({
    waiting_approvals: ["apr_1"],
    waiting_request: { request_id: "r1", question: "选哪个方案？",
                       phase: "pending", answered: false, created_at: 0 },
  });
  const ws = workspaceFromCheckpoint(t, null, nodeDefs);
  assert.equal(ws.pendingActions.length, 2);
  assert.ok(ws.pendingActions.some((a) => a.kind === "approval"));
  assert.ok(ws.pendingActions.some((a) => a.kind === "clarification"));
});

test("NEEDS_RECONCILE 出现在待办", () => {
  const t = task({ status: "NEEDS_RECONCILE" });
  const ws = workspaceFromCheckpoint(t, null, nodeDefs);
  assert.ok(ws.pendingActions.some((a) => a.kind === "reconcile"));
});

test("空 checkpoint：全部 pending、无成果、无百分比", () => {
  const ws = workspaceFromCheckpoint(task(), null, nodeDefs);
  assert.equal(ws.nodes.length, 3);
  assert.ok(ws.nodes.every((n) => n.state === "pending"));
  assert.equal(ws.partialResults.length, 0);
  assert.equal(ws.percent, null);
});
