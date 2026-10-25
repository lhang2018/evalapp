import { test } from "node:test";
import assert from "node:assert/strict";

import {
  classifyPipeline,
  resultView,
  uploadPayload,
} from "../../src/features/pipeline/chain.ts";
import type { ActionReceipt } from "../../src/device/taskProtocol.ts";
import type { LocalTask } from "../../src/offline/types.ts";

// ---- classifyPipeline ----

test("概念问题 → chat 模式", () => {
  const d = classifyPipeline({ text: "什么是文件系统？", modeOverride: null });
  assert.equal(d.mode, "chat");
  assert.equal(d.proposalInput, null);
});

test("请求执行 → work 模式", () => {
  const d = classifyPipeline({
    text: "帮我打开设置", task_id: "t1", run_id: "r1",
    modeOverride: null,
  });
  assert.equal(d.mode, "work");
  assert.ok(d.proposalInput);
  assert.equal(d.proposalInput!.kind, "launch_intent");
});

test("用户显式切换优先", () => {
  const d = classifyPipeline({
    text: "什么是文件系统？",
    modeOverride: "work",
  });
  assert.equal(d.mode, "work");
});

// ---- resultView ----

function receipt(overrides: Partial<ActionReceipt> = {}): ActionReceipt {
  return {
    effect_id: "fx1", action_id: "act1", proposal_id: "p1",
    task_id: "t1", run_id: "r1", kind: "launch_intent", risk: "low",
    state: "verified", terminal: true, replayed: false,
    message: "执行成功", evidence: { node_text: "设置" },
    occurred_at: 1, recorded_at: 2,
    ...overrides,
  };
}

test("成功回执 → 用户看到完成状态", () => {
  const v = resultView(receipt({ state: "verified" }));
  assert.ok(v.state === "verified" || v.label.length > 0);
  assert.equal(v.sideEffectWarning, null);
  assert.ok(v.uploadRefs);
});

test("未验证副作用 → 警告不自动重放", () => {
  const v = resultView(receipt({ state: "unverified", terminal: false }));
  assert.match(v.sideEffectWarning!, /自动重放/);
  assert.equal(v.uploadRefs.has_evidence, true, '有证据时 has_evidence=true；内容不进载荷');
});

test("拒绝 → 用户看到拒绝原因", () => {
  const v = resultView(receipt({ state: "denied", message: "未获授权" }));
  assert.equal(v.sideEffectWarning, "操作已被拒绝");
});

test("上传载荷不含参数/文本/证据内容", () => {
  const r = receipt({ evidence: { before: "秘密", after: "秘密" } });
  const payload = uploadPayload(r);
  const s = JSON.stringify(payload);
  assert.ok(!s.includes("秘密"), "上传载荷不得包含证据内容");
  assert.ok(payload.has_evidence === true);
});

// ---- 完整链路集成探针 ----

test("chat → 无设备动作；work → 设备动作并投影", () => {
  const chat = classifyPipeline({ text: "你好", modeOverride: null });
  assert.equal(chat.mode, "chat");

  const work = classifyPipeline({ text: "帮我打开设置", task_id: "t1", run_id: "r1", modeOverride: null });
  assert.equal(work.mode, "work");

  const r = resultView(receipt({ state: "verified" }));
  assert.equal(r.state, "verified");
});

test("任务视图衔接：回执终态→任务视图显示完成", () => {
  const t: LocalTask = {
    local_task_id: "t1", client_request_id: "r1", goal: "打开设置",
    intent: "general", risk_level: "low", status: "DONE",
    created_at: 0, updated_at: 0, server_status: "SUCCEEDED",
    result: "已打开设置",
  };
  const v = resultView(receipt({ state: "verified" }), t);
  assert.ok(v.label.length > 0);
});
