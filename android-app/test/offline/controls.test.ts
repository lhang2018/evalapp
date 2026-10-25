import { test } from "node:test";
import assert from "node:assert/strict";

import {
  availableControls,
  checkRecoverySafety,
  planGoalChange,
} from "../../src/features/controls/paths.ts";
import type { LocalTask } from "../../src/offline/types.ts";

function task(overrides: Partial<LocalTask> = {}): LocalTask {
  return {
    local_task_id: "t1", client_request_id: "r1", goal: "x", intent: "general",
    risk_level: "low", status: "SUBMITTED", created_at: 0, updated_at: 0,
    ...overrides,
  };
}

test("执行中任务：可取消和暂停", () => {
  const t = task({ server_status: "RUNNING" });
  const options = availableControls(t);
  assert.ok(options.some((o) => o.action === "cancel"));
  assert.ok(options.some((o) => o.action === "pause"));
  // 取消需要确认
  assert.ok(options.find((o) => o.action === "cancel")!.needsConfirm);
});

test("暂停中任务：可继续（无暂停选项）", () => {
  const t = task({ server_status: "SUSPENDED" });
  const options = availableControls(t);
  const resume = options.find((o) => o.action === "resume");
  assert.ok(resume, "暂停中应有继续选项");
  assert.equal(resume!.needsConfirm, false);
  assert.ok(!options.some((o) => o.action === "pause"));
});

test("终态任务：无控制动作", () => {
  for (const server of ["SUCCEEDED", "FAILED", "CANCELLED"]) {
    const options = availableControls(task({ server_status: server }));
    assert.equal(options.length, 0, server);
  }
});

test("NEEDS_RECONCILE：确认重提选项", () => {
  const t = task({ status: "NEEDS_RECONCILE" });
  const options = availableControls(t);
  const reconcile = options.find((o) => o.action === "confirm_reconcile");
  assert.ok(reconcile);
  assert.ok(reconcile!.needsConfirm);
});

test("目标变更：新目标创建新任务，旧任务保留", () => {
  const plan = planGoalChange("旧目标", "新目标");
  assert.equal(plan.newGoal, "新目标");
  assert.match(plan.reason, /变更/);
  assert.equal(plan.requiresReapproval, true, "目标变更需重新审批");
  // 未变更
  const same = planGoalChange("同一目标", "同一目标");
  assert.equal(same.requiresReapproval, false);
});

test("安全恢复：未决副作用阻断，已决放行", () => {
  const blocked = checkRecoverySafety([
    { effect_id: "fx1", status: "unknown" },
  ]);
  assert.equal(blocked.safe, false);
  assert.match(blocked.blocker!, /对账/);
  assert.equal(blocked.pendingEffects.length, 1);

  const safe = checkRecoverySafety([
    { effect_id: "fx1", status: "verified" },
  ]);
  assert.equal(safe.safe, true);
  assert.equal(safe.blocker, null);
});
