import { test } from "node:test";
import assert from "node:assert/strict";

import {
  artifactDownloadPath,
  previewNotice,
} from "../../src/features/artifacts/panel.ts";

const meta = {
  artifact_id: "t1:n1:abcd",
  node_id: "n1",
  kind: "markdown",
  sha256: "a".repeat(64),
  bytes: 9000,
};

test("有真实文件时给出全量下载路径并做编码", () => {
  const path = artifactDownloadPath("task 1", meta);
  assert.ok(path);
  assert.ok(path.includes("/artifacts/t1%3An1%3Aabcd/download"));
  assert.ok(path.startsWith("/tasks/task%201/"));
});

test("无文件/无摘要/无任务时不给下载入口", () => {
  assert.equal(artifactDownloadPath("t1", { ...meta, bytes: 0 }), null);
  assert.equal(artifactDownloadPath("t1", { ...meta, sha256: "" }), null);
  assert.equal(artifactDownloadPath("", meta), null);
});

test("截断预览提示可全量下载，未截断保持原提示", () => {
  const truncated = previewNotice({
    artifact_id: "a", kind: "markdown", bytes: 9000, truncated: true,
    text: "x", base64: null,
  });
  assert.ok(truncated.includes("全量下载"));

  const full = previewNotice({
    artifact_id: "a", kind: "markdown", bytes: 100, truncated: false,
    text: "x", base64: null,
  });
  assert.equal(full, "已打开文本预览");
});
