import { test } from "node:test";
import assert from "node:assert/strict";

import {
  canExecute,
  projectUserCapabilities,
} from "../../src/features/capabilities/status.ts";
import type { CapabilitiesStatusPayload } from "../../src/features/capabilities/status.ts";

function payload(
  caps: Array<Partial<CapabilitiesStatusPayload["capabilities"][number]>>,
): CapabilitiesStatusPayload {
  return {
    capabilities: caps.map((c) => ({
      tool_id: "t",
      version: "v1",
      state: "ready" as const,
      reason: "",
      note: null,
      high_risk: false,
      ...c,
    })),
    counts: {},
  };
}

test("六档服务端状态逐档投影为用户视图与恢复路径", () => {
  const views = projectUserCapabilities(payload([
    { tool_id: "a_ready", state: "ready", reason: "健康探针通过" },
    { tool_id: "b_perm", state: "permission_required", reason: "需要确认" },
    { tool_id: "c_denied", state: "denied", reason: "策略已拒绝" },
    { tool_id: "d_dep", state: "dependency_missing", reason: "探针失败" },
    { tool_id: "e_unver", state: "unverified", reason: "未声明探针" },
    { tool_id: "f_unreg", state: "unregistered", reason: "未注册" },
  ]), { accessibilityAvailable: true });
  const by = new Map(views.map((v) => [v.tool_id, v]));
  assert.equal(by.get("a_ready")!.state, "ready");
  assert.equal(by.get("a_ready")!.blocked, false);
  assert.equal(by.get("b_perm")!.state, "needs_permission");
  assert.ok(by.get("b_perm")!.recovery!.includes("确认"));
  assert.equal(by.get("c_denied")!.state, "blocked");
  assert.equal(by.get("c_denied")!.blocked, true);
  assert.equal(by.get("d_dep")!.state, "needs_setup");
  assert.equal(by.get("e_unver")!.state, "unknown");
  assert.equal(by.get("f_unreg")!.state, "blocked");
});

test("未知状态不伪装可用：fallback 归为 unknown", () => {
  const views = projectUserCapabilities(
    payload([{ tool_id: "x", state: "mystery" as never }]),
    { accessibilityAvailable: true },
  );
  assert.equal(views[0].state, "unknown");
  assert.equal(canExecute(views[0]), false);
});

test("拒绝/撤销后受保护动作不能执行：blocked 守卫", () => {
  const views = projectUserCapabilities(
    payload([{ tool_id: "blocked_tool", state: "denied" }]),
    { accessibilityAvailable: true },
  );
  assert.equal(canExecute(views[0]), false);
});

test("设备动作能力在本机无障碍未开启时覆盖为待设置", () => {
  const views = projectUserCapabilities(
    payload([{ tool_id: "open_app", state: "ready" }]),
    { accessibilityAvailable: false },
  );
  assert.equal(views[0].state, "needs_setup");
  assert.ok(views[0].reason!.includes("无障碍"));
  assert.equal(views[0].recovery, "到系统设置开启无障碍服务");
  assert.equal(canExecute(views[0]), false);
  // 本机已授权时保持 ready
  const ok = projectUserCapabilities(
    payload([{ tool_id: "open_app", state: "ready" }]),
    { accessibilityAvailable: true },
  );
  assert.equal(ok[0].state, "ready");
});
