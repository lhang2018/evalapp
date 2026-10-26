import { test } from "node:test";
import assert from "node:assert/strict";

import {
  buildArtifactRows,
  emptyTaskIdHint,
  panelHint,
  previewNotice,
} from "../../src/features/artifacts/panel.ts";
import type {
  TaskArtifactMeta,
  TaskArtifactPreview,
} from "../../src/api.ts";

function meta(overrides: Partial<TaskArtifactMeta> = {}): TaskArtifactMeta {
  return {
    artifact_id: "art-1",
    node_id: "node-write",
    kind: "markdown",
    sha256: "a".repeat(64),
    bytes: 120,
    description: "验收报告",
    created_at: 1,
    ...overrides,
  };
}

test("有真实文件的产物可打开并带追溯信息", () => {
  const rows = buildArtifactRows([meta()]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].openable, true);
  assert.equal(rows[0].trace, "节点 node-write · 产物 art-1");
  assert.equal(rows[0].kindLabel, "Markdown 文档");
});

test("无文件/无摘要的产物不显示打开入口", () => {
  const rows = buildArtifactRows([
    meta({ bytes: 0 }),
    meta({ artifact_id: "art-2", sha256: "" }),
  ]);
  assert.equal(rows[0].openable, false);
  assert.equal(rows[1].openable, false);
});

test("文本预览截断如实标注", () => {
  const preview: TaskArtifactPreview = {
    artifact_id: "art-1",
    kind: "markdown",
    bytes: 8192,
    truncated: true,
    text: "# 报告",
    base64: null,
  };
  assert.match(previewNotice(preview), /截断/);
});

test("二进制预览不当作文本展示", () => {
  const preview: TaskArtifactPreview = {
    artifact_id: "art-1",
    kind: "binary",
    bytes: 10,
    truncated: false,
    text: null,
    base64: "AAEC",
  };
  assert.match(previewNotice(preview), /base64/);
});

test("空结果提示为无成果而不是已交付", () => {
  assert.match(
    panelHint({ offline: false, loading: false, error: null, count: 0 }) ?? "",
    /空结果不视为已交付/,
  );
  assert.match(
    panelHint({ offline: false, loading: false, error: "HTTP 500", count: 0 }) ?? "",
    /加载失败/,
  );
  assert.match(
    panelHint({ offline: true, loading: false, error: null, count: 0 }) ?? "",
    /离线/,
  );
  assert.equal(
    panelHint({ offline: false, loading: false, error: null, count: 2 }),
    null,
  );
});

test("未关联服务端任务时明确提示无成果可查", () => {
  assert.match(emptyTaskIdHint(null) ?? "", /无成果可查/);
  assert.equal(emptyTaskIdHint("srv-1"), null);
});
