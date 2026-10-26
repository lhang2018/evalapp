import { test } from "node:test";
import assert from "node:assert/strict";

import {
  buildMemoryRows,
  deletionReport,
  deniedScopesAfter,
  memoryControlError,
  scopePolicyView,
} from "../../src/features/memory/control.ts";
import type {
  MemoryControlItem,
  MemoryDeleteResult,
  MemoryScopePolicy,
} from "../../src/api.ts";

function item(overrides: Partial<MemoryControlItem> = {}): MemoryControlItem {
  return {
    memory_id: "mem-1",
    kind: "preference",
    scope: "user",
    scope_key: "u1",
    status: "verified",
    content: "用户喜欢简洁回答",
    source: "user:profile",
    applicable_domains: [],
    created_at: 1,
    updated_at: 2,
    use_count: 3,
    provenance: { source: "user", scope: "user", task_id: "task-9" },
    ...overrides,
  };
}

function policy(overrides: Partial<MemoryScopePolicy> = {}): MemoryScopePolicy {
  return {
    allow: ["user", "project", "task"],
    deny: [],
    scope_values: ["user", "project", "task", "session"],
    ...overrides,
  };
}

test("记忆行展示来源/范围/状态与任务来源", () => {
  const rows = buildMemoryRows([item()]);
  assert.equal(rows[0].sourceLabel, "用户提供");
  assert.equal(rows[0].scopeLabel, "用户");
  assert.equal(rows[0].statusLabel, "已核实");
  assert.match(rows[0].provenanceNote, /task-9/);
  assert.equal(rows[0].canCorrect, true);
});

test("已撤销记忆不允许纠正但仍可删除", () => {
  const rows = buildMemoryRows([item({ status: "revoked" })]);
  assert.equal(rows[0].canCorrect, false);
  assert.equal(rows[0].canDelete, true);
});

test("删除报告展示保留边界与失败项", () => {
  const result: MemoryDeleteResult = {
    ok: true,
    memory_id: "mem-1",
    retained: [
      { layer: "tombstones", note: "仅摘要" },
      { layer: "历史备份", note: "未追删" },
    ],
    fully_erased: false,
    scope_claim: "仅本地记忆库与检索缓存",
  };
  const report = deletionReport(result);
  assert.equal(report.ok, true);
  assert.match(report.title, /保留项/);
  assert.equal(report.retainedLines.length, 2);
  assert.match(report.text, /不宣称全域擦除/);
});

test("删除失败与部分失败明确可见", () => {
  const failed = deletionReport({
    ok: false,
    memory_id: "mem-1",
    error: "数据库忙",
    retained: [],
  });
  assert.equal(failed.ok, false);
  assert.match(failed.text, /数据库忙/);
  const partial = deletionReport({
    ok: true,
    memory_id: "mem-1",
    failures: [{ stage: "delete_chain", error: "链未执行" }],
    retained: [],
  });
  assert.equal(partial.failureLines.length, 1);
});

test("离线与权限错误有明确提示", () => {
  assert.match(memoryControlError(null, false) ?? "", /离线/);
  assert.match(memoryControlError({ status: 401 }, true) ?? "", /管理员令牌/);
  assert.match(memoryControlError({ status: 403 }, true) ?? "", /无权限/);
  assert.equal(memoryControlError(null, true), null);
});

test("范围策略展示已禁用范围", () => {
  assert.match(scopePolicyView(policy()).text, /所有范围均参与分析/);
  const denied = scopePolicyView(policy({ deny: ["session"], allow: ["user"] }));
  assert.equal(denied.disabled[0], "session");
  assert.match(denied.text, /已禁用范围/);
});

test("禁用/恢复范围校验", () => {
  const disabled = deniedScopesAfter(policy(), "session", true);
  assert.equal(disabled.ok, true);
  assert.deepEqual(disabled.deny, ["session"]);
  const restored = deniedScopesAfter(
    policy({ deny: ["session"] }),
    "session",
    false,
  );
  assert.deepEqual(restored.deny, []);
  const unknown = deniedScopesAfter(policy(), "unknown_scope", true);
  assert.equal(unknown.ok, false);
  const empty = deniedScopesAfter(
    { allow: [], deny: [], scope_values: ["user"] },
    "user",
    true,
  );
  assert.equal(empty.ok, true);
});
