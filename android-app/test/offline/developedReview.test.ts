import { test } from "node:test";
import assert from "node:assert/strict";
import { classifyMode, buildTaskSeed } from "../../src/features/modes/transition.ts";
import { taskViewOf, waitReasonOf } from "../../src/features/taskview/state.ts";
import type { LocalTask } from "../../src/offline/types.ts";

test("概念问答包含文件/报告/运行词仍应保持轻量聊天", () => {
  for (const input of ["什么是文件？", "帮我解释一下报告是什么", "如何运行测试？", "为什么需要自动化？"]) {
    assert.equal(classifyMode(input).mode, "chat", input);
  }
  assert.equal(classifyMode("解释一下文件格式，然后帮我生成报告").mode, "work");
  assert.equal(classifyMode("能帮我打开设置吗？").mode, "work");
});

test("过去的纠正不覆盖后来明确的新目标，约束不被当目标", () => {
  const seed = buildTaskSeed([
    { role: "user", text: "改成财务部分", correction: true },
    { role: "user", text: "把这些整理成报告" },
    { role: "user", text: "不超过三页", confirmed: true },
  ]);
  assert.equal(seed.goal, "把这些整理成报告");
  assert.deepEqual(seed.corrections, ["改成财务部分"]);
  assert.deepEqual(seed.constraints, ["不超过三页"]);
});

test("服务端终态不保留过期审批或网络等待动作", () => {
  for (const status of ["SUCCEEDED", "FAILED", "CANCELLED"]) {
    const view = taskViewOf({
      local_task_id: "t1", client_request_id: "r1", user_goal: "x",
      goal: "x", intent: "general", risk_level: "low", created_at: 0, updated_at: 0,
      status: "DONE", server_status: status, waiting_approvals: ["old"],
      last_sync_error: "old timeout",
    } as LocalTask);
    assert.equal(view.wait.reason, null);
    assert.equal(view.wait.needsUserAction, false);
  }
});

test("已回答的澄清不能继续要求用户补充", () => {
  assert.equal(waitReasonOf({
    server_status: "RUNNING",
    waiting_request: { request_id: "old", answered: true },
  }).needsUserAction, false);
});
