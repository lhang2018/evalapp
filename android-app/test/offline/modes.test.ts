import { test } from "node:test";
import assert from "node:assert/strict";

import {
  buildTaskSeed,
  classifyMode,
  noRepeatGuard,
} from "../../src/features/modes/transition.ts";

test("模式判定：显式切换优先于输入特征", () => {
  const d = classifyMode("帮我整理文件", { explicit: "chat" });
  assert.equal(d.mode, "chat");
  assert.equal(d.source, "user_explicit");
});

test("模式判定：请求代为执行/需要产物/需要工具 → work", () => {
  for (const text of ["帮我比较三个方案并整理成报告",
                      "把这个保存成文件",
                      "帮我打开设置"]) {
    const d = classifyMode(text);
    assert.equal(d.mode, "work", text);
    assert.ok(d.signal);
  }
});

test("模式判定：直接问答轻量完成 → chat", () => {
  for (const text of ["什么是幂等性？", "解释一下这个概念",
                      "你觉得这个设计怎么样？"]) {
    assert.equal(classifyMode(text).mode, "chat", text);
  }
  assert.equal(classifyMode("").mode, "chat");
});

test("聊天转工作：goal 取最后目标；纠正口径优先", () => {
  const seed = buildTaskSeed([
    { role: "user", text: "帮我做一份周报" },
    { role: "assistant", text: "好的，包含哪些部分？" },
    { role: "user", text: "不要市场部分，改成财务部分", correction: true },
  ]);
  assert.equal(seed.goal, "不要市场部分，改成财务部分");
  assert.ok(seed.corrections.includes("不要市场部分，改成财务部分"));
});

test("聊天转工作：仅确认过的讨论升级为约束", () => {
  const seed = buildTaskSeed([
    { role: "user", text: "用中文写，不超过三页", confirmed: true },
    { role: "user", text: "也许可以加个附录（还没定）" },
  ]);
  assert.equal(seed.constraints.length, 1);
  assert.ok(seed.constraints[0].includes("三页"));
});

test("聊天转工作：已完成要点只进摘要，不重复执行", () => {
  const seed = buildTaskSeed([
    { role: "user", text: "先把格式定为表格", completed: true },
    { role: "user", text: "再把数据填进去" },
  ]);
  assert.ok(seed.completedDigest.some((s) => s.includes("表格")));
  assert.ok(!seed.pendingPoints.some((s) => s.includes("表格")));
  const guard = noRepeatGuard(seed);
  assert.equal(guard.ok, true);
  // 若把已完成事项误放待办，守卫必须拦下
  const bad = { ...seed, pendingPoints: ["先把格式定为表格", ...seed.pendingPoints] };
  const g2 = noRepeatGuard(bad);
  assert.equal(g2.ok, false);
  assert.equal(g2.repeats.length, 1);
});

test("聊天转工作：讨论≠执行——assistant 结论不产生待办", () => {
  const seed = buildTaskSeed([
    { role: "user", text: "帮我出方案" },
    { role: "assistant", text: "我建议分三步：收集、整理、交付" },
  ]);
  assert.equal(seed.goal, "帮我出方案");
  assert.equal(seed.pendingPoints.filter((p) => p.includes("三步")).length, 0);
});
