import { test } from "node:test";
import assert from "node:assert/strict";

import {
  formatBytes,
  rebuildPlanView,
  toCardView,
  truncationHint,
} from "../../src/features/artifacts/view.ts";
import type { ServerArtifactMeta } from "../../src/features/artifacts/view.ts";

const meta: ServerArtifactMeta = {
  artifact_id: "t1:report:abc123def456",
  node_id: "report",
  kind: "markdown",
  sha256: "abc123def4567890abcdef",
  bytes: 4096,
  description: "计算报告",
  created_at: 1700000000,
};

test("toCardView：类型标签/大小格式/digest/可下载", () => {
  const card = toCardView(meta);
  assert.equal(card.kindLabel, "Markdown 文档");
  assert.equal(card.sizeLabel, "4.0 KB");
  assert.ok(card.digestLabel.startsWith("sha256:abc123"));
  assert.equal(card.downloadable, true);
  assert.equal(card.description, "计算报告");
});

test("无 sha256 或零字节不显示下载", () => {
  assert.equal(toCardView({ ...meta, sha256: "" }).downloadable, false);
  assert.equal(toCardView({ ...meta, bytes: 0 }).downloadable, false);
});

test("预览文本与截断提示", () => {
  const card = toCardView(meta, {
    artifact_id: meta.artifact_id, kind: "markdown", bytes: 8192,
    truncated: true, text: "# 报告…", base64: null,
  });
  assert.equal(card.previewText, "# 报告…");
  assert.equal(card.truncated, true);
  assert.equal(truncationHint(card), "预览已截断，下载查看全文");
  // 无截断
  assert.equal(truncationHint(toCardView(meta, {
    artifact_id: meta.artifact_id, kind: "markdown", bytes: 100,
    truncated: false, text: "短内容", base64: null,
  })), null);
});

test("formatBytes 格式", () => {
  assert.equal(formatBytes(500), "500 B");
  assert.equal(formatBytes(2048), "2.0 KB");
  assert.equal(formatBytes(5 * 1024 * 1024), "5.0 MB");
});

test("rebuildPlanView：只重做受影响产物", () => {
  const plan = rebuildPlanView(["b", "c"], ["a"]);
  assert.deepEqual(plan.rebuild, ["b", "c"]);
  assert.deepEqual(plan.keep, ["a"]);
  assert.match(plan.summary, /重新生成 2 项/);
  assert.match(plan.summary, /保留不变 1 项/);
});

test("rebuildPlanView：无变更", () => {
  const plan = rebuildPlanView([], []);
  assert.equal(plan.summary, "无变更");
});
