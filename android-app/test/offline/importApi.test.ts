import { test } from "node:test";
import assert from "node:assert/strict";

import { EvoAgentClient } from "../../src/api.ts";

interface Capture {
  url?: string;
  init?: RequestInit;
}

function stubFetch(capture: Capture, status: number, body: unknown) {
  const original = globalThis.fetch;
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    capture.url = url;
    capture.init = init;
    return {
      ok: status < 400,
      status,
      json: async () => body,
    } as Response;
  }) as typeof fetch;
  return () => {
    globalThis.fetch = original;
  };
}

test("diagnostics import posts raw zip with an admin token", async () => {
  const capture: Capture = {};
  const restore = stubFetch(capture, 201, {
    import: {
      import_id: "imp_1",
      file_name: "device.zip",
      size_bytes: 2,
      sha256: "a".repeat(64),
      format_version: "1.0",
      entry_count: 6,
      event_count: 3,
      status: "quarantined",
      imported_at: 1,
    },
    duplicate: false,
    needs_review: 1,
  });
  try {
    const client = new EvoAgentClient("http://example.test/");
    const result = await client.importDiagnosticsPackage(
      "../诊断包.zip",
      new Uint8Array([0x50, 0x4b]),
      "admin-token",
    );
    assert.equal(capture.url, "http://example.test/diagnostics/import");
    assert.equal(capture.init?.method, "POST");
    const headers = capture.init?.headers as Record<string, string>;
    assert.equal(headers["Content-Type"], "application/zip");
    assert.equal(headers.Authorization, "Bearer admin-token");
    assert.match(headers["X-File-Name"], /^[\x20-\x7e]+\.zip$/);
    assert.equal(result.duplicate, false);
    assert.equal(result.needs_review, 1);
  } finally {
    restore();
  }
});

test("import errors keep the server status and message", async () => {
  const capture: Capture = {};
  const restore = stubFetch(capture, 403, {
    error: "该操作需要 admin 权限",
  });
  try {
    const client = new EvoAgentClient("http://example.test");
    await assert.rejects(
      client.importDiagnosticsPackage("device.zip", new Uint8Array([1])),
      (error: Error & { status?: number }) => {
        assert.equal(error.status, 403);
        assert.match(error.message, /admin/);
        return true;
      },
    );
    const headers = capture.init?.headers as Record<string, string>;
    assert.equal(headers.Authorization, undefined);
  } finally {
    restore();
  }
});
