import { test } from "node:test";
import assert from "node:assert/strict";

import { MemoryStore } from "../../src/offline/storage.ts";
import { InstallJournal } from "../../src/install/journal.ts";
import {
  flushInstallReceipts,
  reportInstall,
  type InstallReceiptTransport,
} from "../../src/install/report.ts";
import { retireRelease } from "../../src/install/retire.ts";
import type { ApplyResultRequest } from "../../src/update/types.ts";

class RecordingTransport implements InstallReceiptTransport {
  readonly posted: ApplyResultRequest[] = [];
  failNext = 0;

  async applyResult(request: ApplyResultRequest): Promise<void> {
    if (this.failNext > 0) {
      this.failNext -= 1;
      throw new Error("网络中断");
    }
    this.posted.push(request);
  }
}

const base = {
  release_id: "rel_9_1_0",
  version: "v9.1.0",
  artifact_digest: "cd".repeat(32),
  signing_key_id: "rel-key-1",
  device_id: "dev_001",
};

function journal() {
  return new InstallJournal(new MemoryStore(), {
    now: (() => 1_700_000_000_000),
  });
}

test("退役成功：回执绑定设备/版本/制品并确认", async () => {
  const j = journal();
  const transport = new RecordingTransport();
  const result = await retireRelease(j, transport, {
    ...base, reason: "能力下线",
  });
  assert.equal(result.ok, true);
  assert.equal(result.duplicate, false);
  assert.equal(transport.posted.length, 1);
  const posted = transport.posted[0];
  assert.equal(posted.action, "retired");
  assert.equal(posted.release_id, "rel_9_1_0");
  assert.equal(posted.version, "v9.1.0");
  assert.equal(posted.artifact_digest, "cd".repeat(32));
  assert.equal(posted.signing_key_id, "rel-key-1");
  assert.equal(posted.reason, "能力下线");
  assert.match(posted.apply_id, /^[A-Za-z0-9_-]{8,64}$/);
  const status = await j.status();
  assert.equal(status.trust, "retired");
  assert.equal(status.active_release_id, null);
});

test("退役上传失败：未确认，不冒充已退役", async () => {
  const j = journal();
  const transport = new RecordingTransport();
  transport.failNext = 1;
  const result = await retireRelease(j, transport, {
    ...base, reason: "能力下线",
  });
  assert.equal(result.ok, false);
  assert.equal(transport.posted.length, 0);
  const status = await j.status();
  assert.equal(status.trust, "retired_unconfirmed");
  assert.equal(status.pending_apply_ids.length, 1);
});

test("退役必须写明原因", async () => {
  const j = journal();
  const transport = new RecordingTransport();
  const result = await retireRelease(j, transport, {
    ...base, reason: "   ",
  });
  assert.equal(result.ok, false);
  assert.match(result.error ?? "", /原因/);
  assert.equal(transport.posted.length, 0);
  assert.equal((await j.status()).trust, "none");
});

test("重复退役返回待确认条目且不重复上报", async () => {
  const j = journal();
  const transport = new RecordingTransport();
  transport.failNext = 1;
  const first = await retireRelease(j, transport, {
    ...base, reason: "能力下线",
  });
  assert.equal(first.ok, false);
  const second = await retireRelease(j, transport, {
    ...base, reason: "能力下线",
  });
  assert.equal(second.duplicate, true);
  assert.equal(second.entry?.apply_id, first.entry?.apply_id);
  assert.equal(transport.posted.length, 0);
  // 网络恢复后补传只发一条
  const remaining = await flushInstallReceipts(j, transport);
  assert.deepEqual(remaining, []);
  assert.equal(transport.posted.length, 1);
  assert.equal((await j.status()).trust, "retired");
});

test("reportInstall 上传 activated 回执；失败留待补传", async () => {
  const j = journal();
  const transport = new RecordingTransport();
  transport.failNext = 1;
  const failed = await reportInstall(j, transport, {
    kind: "activated", ...base,
  });
  assert.equal(failed.ok, false);
  assert.equal((await j.status()).trust, "installed_unconfirmed");
  const remaining = await flushInstallReceipts(j, transport);
  assert.deepEqual(remaining, []);
  assert.equal(transport.posted.length, 1);
  assert.equal(transport.posted[0].action, "activated");
  assert.equal((await j.status()).trust, "installed_confirmed");
});

test("reportInstall 断联时 ok=false 且不写确认", async () => {
  const j = journal();
  const transport = new RecordingTransport();
  transport.failNext = 1;
  const result = await reportInstall(j, transport, {
    kind: "installed", ...base,
  });
  assert.equal(result.ok, false);
  const entries = await j.entries();
  assert.equal(entries.length, 1);
  assert.equal(entries[0].server_confirmed, false);
  assert.equal(entries[0].kind, "installed");
});
