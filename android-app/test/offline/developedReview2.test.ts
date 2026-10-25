import { test } from "node:test";
import assert from "node:assert/strict";
import { availableControls, checkRecoverySafety } from "../../src/features/controls/paths.ts";
import { budgetStatusView, formatCost } from "../../src/features/budget/view.ts";
import type { LocalTask } from "../../src/offline/types.ts";

test("服务端终态覆盖NEEDS_RECONCILE旧缓存，不允许再次确认重提", () => {
  for (const server_status of ["SUCCEEDED", "FAILED", "CANCELLED"]) {
    assert.deepEqual(availableControls({
      status: "NEEDS_RECONCILE", server_status,
    } as LocalTask), []);
  }
});

test("planned与未知副作用状态均不能宣称安全恢复", () => {
  for (const status of ["planned", "submitted", "unknown", "not_a_ledger_state"]) {
    assert.equal(checkRecoverySafety([{effect_id:"fx",status}]).safe,false,status);
  }
  for (const status of ["confirmed", "failed", "compensated"]) {
    assert.equal(checkRecoverySafety([{effect_id:"fx",status}]).safe,true,status);
  }
});

test("预算上限未知不能显示充足；零预算是硬上限", () => {
  assert.notEqual(budgetStatusView(undefined,undefined).level,"ok");
  assert.equal(budgetStatusView({model_calls:0},0).blocked,true);
});

test("未核对产物时不能宣称产物已保留，异常费用不得显示成金额", () => {
  assert.notEqual(budgetStatusView({model_calls:3},3).artifactsPreserved,true);
  for (const cost of [Number.NaN,Number.POSITIVE_INFINITY,-1]) {
    assert.equal(formatCost(cost),"—");
  }
});
