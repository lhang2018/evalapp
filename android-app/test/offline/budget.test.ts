import { test } from "node:test";
import assert from "node:assert/strict";

import {
  budgetStatusView,
  formatCalls,
  formatCost,
  taskBudgetView,
  unknownCostView,
} from "../../src/features/budget/view.ts";
import type { LocalTask } from "../../src/offline/types.ts";

test("formatCost / formatCalls 基本格式", () => {
  assert.equal(formatCost(0.123, "CNY"), "¥0.123");
  assert.match(formatCost(0.123), /币种未知/);
  assert.equal(formatCost(0.123, "USD"), "$0.123");
  assert.equal(formatCost(undefined), "—");
  assert.equal(formatCalls(5), "5 次调用");
  assert.equal(formatCalls(undefined), "0 次调用");
});

test("unknown 不能当 0 金额：单独标注", () => {
  const v = unknownCostView({ unknown_costs: 3, cost: 1.5 });
  assert.equal(v.hasUnknown, true);
  assert.match(v.label, /3 项未知成本/);
  assert.match(v.label, /不按 0 元释放/);
  // 无未知时
  assert.equal(unknownCostView({ unknown_costs: 0 }).hasUnknown, false);
  assert.equal(unknownCostView({}).hasUnknown, false);
});

test("预算状态三档：ok / warning(≥80%) / exhausted(=100%)", () => {
  const ok = budgetStatusView({ model_calls: 5 }, 10);
  assert.equal(ok.level, "ok");
  assert.equal(ok.blocked, false);

  const warn = budgetStatusView({ model_calls: 8 }, 10);
  assert.equal(warn.level, "warning");
  assert.equal(warn.blocked, false);

  const exhausted = budgetStatusView({ model_calls: 10 }, 10);
  assert.equal(exhausted.level, "exhausted");
  assert.equal(exhausted.blocked, true);
  assert.equal(exhausted.artifactsPreserved, null);
  assert.match(exhausted.label, /耗尽/);
});

test("预算耗尽不推断产物已保留；独立核验结果单独传入", () => {
  const v = budgetStatusView({ model_calls: 10 }, 10);
  assert.equal(v.artifactsPreserved, null, "预算信息不能证明文件仍存在");
  assert.equal(budgetStatusView({ model_calls: 10 }, 10, true).artifactsPreserved, true);
});

test("maxModelCalls 未定义时如实显示未知", () => {
  const v = budgetStatusView({ model_calls: 100 }, undefined);
  assert.equal(v.level, "unknown");
});

test("taskBudgetView：needs_you 状态透传", () => {
  const t: LocalTask = {
    local_task_id: "t1", client_request_id: "r1", goal: "x", intent: "general",
    risk_level: "low", status: "SUBMITTED", created_at: 0, updated_at: 0,
    server_status: "WAITING_USER",
  };
  const v = taskBudgetView(t);
  assert.equal(v.needsUser, true);
});
