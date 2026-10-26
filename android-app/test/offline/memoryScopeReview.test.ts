import { test } from "node:test";
import assert from "node:assert/strict";
import { deniedScopesAfter, scopePolicyView } from "../../src/features/memory/control.ts";

const initial = { allow: ["user", "task"], deny: [], scope_values: ["user", "task", "shared"] };

test("禁用再恢复将范围放回允许列表，不开放其他范围", () => {
  const disabled = deniedScopesAfter(initial, "user", true);
  assert.equal(disabled.ok, true);
  assert.deepEqual(disabled.allow, ["task"]);
  const restored = deniedScopesAfter({ ...initial, allow: disabled.allow, deny: disabled.deny }, "user", false);
  assert.equal(restored.ok, true);
  assert.deepEqual([...restored.allow].sort(), ["task", "user"]);
  assert.deepEqual(restored.deny, []);
  assert.equal(restored.allow.includes("shared"), false);
});

test("allow未包含的范围同样显示为禁用", () => {
  const view = scopePolicyView(initial);
  assert.deepEqual(view.disabled, ["shared"]);
  assert.doesNotMatch(view.text, /所有范围均参与/);
});

test("策略读取失败不能显示全部启用或允许提交", () => {
  const view = scopePolicyView(null);
  assert.equal(view.manageable, false);
  assert.match(view.text, /未加载|未知|失败/);
  assert.equal(deniedScopesAfter(null, "user", true).ok, false);
});
