import { test } from "node:test";
import assert from "node:assert/strict";

import {
  isTerminalServerStatus,
  taskViewOf,
} from "../../src/features/taskview/state.ts";
import type { LocalTask } from "../../src/offline/types.ts";

function task(overrides: Partial<LocalTask>): LocalTask {
  return {
    local_task_id: "t1",
    client_request_id: "req_t1",
    goal: "目标",
    intent: "general",
    risk_level: "low",
    status: "SUBMITTED",
    created_at: 0,
    updated_at: 0,
    ...overrides,
  };
}

test("服务端九态语义逐态映射，终态判定不受模型文字影响", () => {
  const cases: Array<[string, string]> = [
    ["CREATED", "queued"],
    ["CLASSIFYING", "working"],
    ["PLANNING", "working"],
    ["READY", "queued"],
    ["RUNNING", "working"],
    ["RECOVERING", "working"],
    ["VERIFYING", "working"],
    ["WAITING_USER", "needs_you"],
    ["WAITING_DEPENDENCY", "waiting"],
    ["SUSPENDED", "waiting"],
    ["SUCCEEDED", "done"],
    ["FAILED", "failed"],
    ["CANCELLED", "cancelled"],
  ];
  for (const [server, expected] of cases) {
    const v = taskViewOf(task({ server_status: server,
      status: "SUBMITTED" }));
    assert.equal(v.state, expected, server);
  }
  // 模型文字声称完成，但服务端仍在 RUNNING → 不是 done
  const fake = taskViewOf(task({
    server_status: "RUNNING",
    status: "SUBMITTED",
    result: "模型说自己已经完成了",
  }));
  assert.equal(fake.state, "working");
});

test("终态判定：仅服务端终态有效", () => {
  assert.equal(isTerminalServerStatus("SUCCEEDED"), true);
  assert.equal(isTerminalServerStatus("RUNNING"), false);
});

test("等待原因来自权威字段：审批/澄清/中断/网络", () => {
  const v1 = taskViewOf(task({ server_status: "WAITING_USER",
    waiting_approvals: ["apr_1"] }));
  assert.equal(v1.state, "needs_you");
  assert.match(v1.wait.reason!, /审批/);
  assert.equal(v1.wait.needsUserAction, true);

  const v2 = taskViewOf(task({ server_status: "WAITING_USER",
    waiting_request: { request_id: "r1", question: "需要哪个目录？", phase: "pending", answered: false, created_at: 0 } }));
  assert.match(v2.wait.reason!, /补充信息/);

  const v3 = taskViewOf(task({ status: "NEEDS_RECONCILE" }));
  assert.equal(v3.state, "needs_you");
  assert.match(v3.wait.reason!, /中断/);

  const v4 = taskViewOf(task({ status: "SUBMITTED",
    last_sync_error: "timeout" }));
  assert.match(v4.wait.reason!, /网络/);
  assert.equal(v4.wait.needsUserAction, false);
});

test("离线本地阶段：无服务端状态按本地展示，DONE 不宣称服务端已验证", () => {
  assert.equal(taskViewOf(task({ status: "LOCAL_ONLY" })).label, "正在提交");
  assert.equal(taskViewOf(task({ status: "SUBMITTING" })).state, "queued");
  const localDone = taskViewOf(task({ status: "DONE",
    result: "本地产物" }));
  assert.equal(localDone.state, "done");
  assert.match(localDone.label, /本地/);
  const localFail = taskViewOf(task({ status: "DONE", result: null }));
  assert.equal(localFail.state, "failed");
});

test("暂停任务给出可继续/取消的等待原因", () => {
  const v = taskViewOf(task({ server_status: "SUSPENDED",
    status: "SUBMITTED" }));
  assert.equal(v.state, "waiting");
  assert.match(v.wait.reason!, /暂停/);
});
