import { test } from "node:test";
import assert from "node:assert/strict";

import {
  detectPermissionDialog,
  diffSnapshots,
  normalizeSnapshot,
  verifyAction,
} from "../../src/device/observation.ts";
import type { DeviceSnapshot } from "../../src/device/observation.ts";

const snapshot = (
  packageName: string,
  texts: string[],
): DeviceSnapshot => ({
  package_name: packageName,
  node_count: texts.length,
  truncated: false,
  nodes: texts.map((text) => ({ text, clickable: true })),
});

test("identical snapshots do not verify an action", () => {
  const before = snapshot("com.example.app", ["设置", "返回"]);
  const after = snapshot("com.example.app", ["设置", "返回"]);
  const diff = diffSnapshots(before, after);
  assert.equal(diff.changed, false);
  const result = verifyAction(before, after);
  assert.equal(result.status, "unverified");
  assert.match(result.message, /未发生可观察变化/);
});

test("new text after the action verifies it", () => {
  const before = snapshot("com.example.app", ["设置"]);
  const after = snapshot("com.example.app", ["设置", "已保存"]);
  const diff = diffSnapshots(before, after);
  assert.deepEqual(diff.added, ["已保存"]);
  const result = verifyAction(before, after);
  assert.equal(result.status, "verified");
  assert.equal((result.evidence as Record<string, unknown>).changed, true);
});

test("package switch verifies launch", () => {
  const before = snapshot("com.evoagent", ["EvoAgent 任务中心"]);
  const after = snapshot("com.android.settings", ["设置", "网络"]);
  const result = verifyAction(before, after, {
    expect_package: "com.android.settings",
  });
  assert.equal(result.status, "verified");
  assert.match(result.message, /com\.android\.settings/);
});

test("expected text absent is reported as unverified", () => {
  const before = snapshot("com.example.app", ["设置"]);
  const after = snapshot("com.example.app", ["设置", "完成"]);
  const result = verifyAction(before, after, { expect_text: "已保存" });
  assert.equal(result.status, "unverified");
  assert.match(result.message, /未观察到预期文本/);
});

test("permission dialog package is recognized and blocks verification", () => {
  const before = snapshot("com.example.app", ["设置"]);
  const after = snapshot("com.android.permissioncontroller", [
    "允许 EvoAgent 访问相机吗？",
    "允许",
    "拒绝",
  ]);
  const dialog = detectPermissionDialog(after);
  assert.equal(dialog.detected, true);
  assert.ok(dialog.buttons.includes("允许"));
  const result = verifyAction(before, after);
  assert.equal(result.status, "unverified");
  assert.match(result.message, /权限弹窗/);
});

test("permission dialog by keywords requires allow, deny and permission word", () => {
  const generic = snapshot("com.example.app", ["允许", "拒绝"]);
  assert.equal(detectPermissionDialog(generic).detected, false);

  const dialog = snapshot("com.example.app", [
    "是否允许访问通知权限",
    "允许",
    "拒绝",
  ]);
  const info = detectPermissionDialog(dialog);
  assert.equal(info.detected, true);
  assert.deepEqual(info.buttons, ["允许", "拒绝"]);
});

test("missing snapshots never verify", () => {
  const result = verifyAction(null, snapshot("com.example.app", ["设置"]));
  assert.equal(result.status, "unverified");
  assert.match(result.message, /缺少操作前后界面快照/);
});

test("native snapshots are normalized with limits and empty input rejected", () => {
  const normalized = normalizeSnapshot({
    package_name: "com.example.app",
    node_count: 2,
    truncated: false,
    nodes: [
      {
        text: "  打开  ",
        clickable: true,
        bounds: [0, 0, 10, 10],
        children: [{ desc: "描述", enabled: true }],
      },
      { text: 123 },
    ],
  });
  assert.ok(normalized);
  assert.equal(normalized.package_name, "com.example.app");
  assert.equal(normalized.nodes[0].text, "打开");
  assert.equal(normalized.nodes[0].children?.[0].desc, "描述");
  assert.deepEqual(normalized.nodes[0].bounds, [0, 0, 10, 10]);
  assert.equal(normalizeSnapshot(null), null);
  assert.equal(normalizeSnapshot({ nodes: [] }), null);
});
