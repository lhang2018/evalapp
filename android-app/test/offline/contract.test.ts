import { test } from "node:test";
import assert from "node:assert/strict";

import { contractViewOf } from "../../src/features/taskview/state.ts";

test("契约展示：全字段投影", () => {
  const v = contractViewOf({
    user_goal: "写报告",
    constraints: ["用中文", "不超过三页"],
    acceptance_criteria: [{ criterion: "包含来源" }],
    target_object: "实验数据",
    expected_output: "markdown",
    complexity: "moderate",
    required_tools: ["file_write"],
    required_permissions: ["file"],
    open_questions: ["来源优先级？"],
  });
  assert.equal(v.goal, "写报告");
  assert.deepEqual(v.constraints, ["用中文", "不超过三页"]);
  assert.deepEqual(v.acceptanceCriteria, ["包含来源"]);
  assert.equal(v.targetObject, "实验数据");
  assert.equal(v.hasContract, true);
});

test("空契约：hasContract=false", () => {
  const v = contractViewOf({});
  assert.equal(v.hasContract, false);
  assert.deepEqual(v.constraints, []);
});

test("旧任务（无契约字段）兼容", () => {
  const v = contractViewOf({ user_goal: "旧任务" });
  assert.equal(v.hasContract, false);
  assert.equal(v.goal, "旧任务");
});

test("acceptance_criteria 兼容非标准格式", () => {
  const v = contractViewOf({
    acceptance_criteria: [{ criterion: "结构化条目" }] as Array<Record<string, unknown>>,
  });
  assert.deepEqual(v.acceptanceCriteria, ["纯文本条目", "结构化条目"]);
});
