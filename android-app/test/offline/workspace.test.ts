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

test("六种节点态可辨（服务端权威 node_states）", () => {
  const defs = [
    { node_id: "a", tool: "fetch", deps: [] },
    { node_id: "b", tool: "process", deps: ["a"] },
    { node_id: "c", tool: "gate", deps: ["a"] },
    { node_id: "d", tool: "report", deps: ["b"] },
    { node_id: "e", tool: "alt", deps: ["a"] },
    { node_id: "f", tool: "end", deps: [] },
  ];
  const cp = {
    completed_nodes: ["a"],
    pending_nodes: ["b"],
    node_states: {
      a: "success", b: "pending", c: "failed",
      d: "skipped", e: "waiting_authorization", f: "running",
    },
    workflow_outputs: { a: { data: 1 } },
  };
  const ws = workspaceFromCheckpoint(task(), cp, defs);
  assert.deepEqual(ws.nodes.map((n) => n.state),
    ["succeeded", "pending", "failed", "skipped", "blocked", "running"]);
  // failed 通道接通：failures 非空且指向失败节点
  assert.deepEqual(ws.failures,
    [{ node_id: "c", error: "未知错误" }]);
});

test("未知服务端态按 pending 保守显示（fail-closed）", () => {
  const cp = { node_states: { a: "mystery_state" }, workflow_outputs: {} };
  const ws = workspaceFromCheckpoint(task(), cp, nodeDefs);
  assert.equal(ws.nodes[0].state, "pending");
});

test("依赖失败时下游 blocked（服务端权威判定）", () => {
  // blocked 只能来自服务端 waiting_authorization/waiting_external，
  // 客户端不基于 deps 推断：b 失败时 c 仍按服务端态显示
  const cp = {
    node_states: { a: "success", b: "failed", c: "waiting_authorization" },
    workflow_outputs: {},
  };
  const ws = workspaceFromCheckpoint(task(), cp, nodeDefs);
  assert.equal(ws.nodes[1].state, "failed");
  assert.equal(ws.nodes[2].state, "blocked");
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
