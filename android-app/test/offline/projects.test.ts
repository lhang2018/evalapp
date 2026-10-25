import { test } from "node:test";
import assert from "node:assert/strict";

import {
  continuationContext,
  groupByProject,
  projectSummary,
} from "../../src/features/projects/space.ts";
import type { LocalTask } from "../../src/offline/types.ts";

function task(overrides: Partial<LocalTask> & { local_task_id: string }): LocalTask {
  return {
    client_request_id: "req_" + overrides.local_task_id,
    goal: "x", intent: "general", risk_level: "low",
    status: "SUBMITTED", created_at: 0, updated_at: 0,
    ...overrides,
  };
}

test("按 goal_id 归集项目", () => {
  const tasks = [
    task({ local_task_id: "t1", goal_id: "proj-a", goal: "任务A" }),
    task({ local_task_id: "t2", goal_id: "proj-a", goal: "任务B" }),
    task({ local_task_id: "t3", goal_id: "proj-b", goal: "任务C" }),
  ];
  const groups = groupByProject(tasks);
  assert.equal(groups.get("proj-a")?.length, 2);
  assert.equal(groups.get("proj-b")?.length, 1);
});

test("无 goal_id 时按 intent 归集", () => {
  const tasks = [
    task({ local_task_id: "t1", intent: "note_taking" }),
    task({ local_task_id: "t2", intent: "note_taking" }),
  ];
  const groups = groupByProject(tasks);
  assert.equal(groups.get("note_taking")?.length, 2);
});

test("项目摘要：任务数/完成数/约束去重", () => {
  const tasks = [
    task({ local_task_id: "t1", status: "DONE",
           contract: { constraints: ["用中文"] } }),
    task({ local_task_id: "t2", status: "SUBMITTED",
           contract: { constraints: ["用中文", "不超过三页"] } }),
  ];
  const s = projectSummary("p1", "测试项目", tasks);
  assert.equal(s.task_count, 2);
  assert.equal(s.done_count, 1);
  assert.equal(s.active_count, 1);
  assert.deepEqual(s.constraints.sort(), ["不超过三页", "用中文"]);
});

test("跨会话延续：目标/约束/产物/未完成任务", () => {
  const tasks = [
    task({ local_task_id: "t1", goal: "写报告", status: "DONE",
           result: "报告内容",
           result_evidence: [
             { artifact_id: "t1:a:abc", node_id: "a", sha256: "x" },
           ],
           contract: { constraints: ["用中文"] } }),
    task({ local_task_id: "t2", goal: "修改报告", status: "SUBMITTED" }),
  ];
  const ctx = continuationContext("p1", tasks);
  assert.ok(ctx.goals.includes("写报告"));
  assert.ok(ctx.constraints.includes("用中文"));
  assert.equal(ctx.artifacts.length, 1);
  assert.equal(ctx.openTasks.length, 1);
  assert.equal(ctx.openTasks[0].goal, "修改报告");
});

test("跨会话延续：全部完成时 openTasks 为空", () => {
  const tasks = [
    task({ local_task_id: "t1", goal: "已完成", status: "DONE",
           result: "done" }),
  ];
  const ctx = continuationContext("p1", tasks);
  assert.equal(ctx.openTasks.length, 0);
  assert.equal(ctx.goals.length, 1);
});
