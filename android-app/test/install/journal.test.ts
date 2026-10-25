import { test } from "node:test";
import assert from "node:assert/strict";

import { MemoryStore } from "../../src/offline/storage.ts";
import { InstallJournal } from "../../src/install/journal.ts";

function journal(store = new MemoryStore()) {
  return new InstallJournal(store, { now: (() => 1_700_000_000_000) });
}

const base = {
  release_id: "rel_9_1_0",
  version: "v9.1.0",
  artifact_digest: "ab".repeat(32),
  signing_key_id: "rel-key-1",
  device_id: "dev_001",
};

test("确认后为 installed_confirmed", async () => {
  const j = journal();
  const entry = await j.record({ ...base, kind: "activated" });
  assert.equal(entry.server_confirmed, false);
  await j.confirm(entry.apply_id);
  const status = await j.status();
  assert.equal(status.trust, "installed_confirmed");
  assert.equal(status.active_release_id, "rel_9_1_0");
  assert.deepEqual(status.pending_apply_ids, []);
});

test("断联未确认不得冒充已安装", async () => {
  const j = journal();
  await j.record({ ...base, kind: "activated" });
  const status = await j.status();
  assert.equal(status.trust, "installed_unconfirmed");
  assert.equal(status.pending_apply_ids.length, 1);
  // 未确认时仍登记活动版本，但信任态明确区分
  assert.equal(status.active_release_id, "rel_9_1_0");
});

test("退役（已确认）后无活动版本", async () => {
  const j = journal();
  const activated = await j.record({ ...base, kind: "activated" });
  await j.confirm(activated.apply_id);
  const retired = await j.record({
    ...base, kind: "retired", reason: "能力下线",
  });
  await j.confirm(retired.apply_id);
  const status = await j.status();
  assert.equal(status.trust, "retired");
  assert.equal(status.active_release_id, null);
  assert.equal(status.active_version, null);
});

test("退役未确认为 retired_unconfirmed", async () => {
  const j = journal();
  const activated = await j.record({ ...base, kind: "activated" });
  await j.confirm(activated.apply_id);
  await j.record({ ...base, kind: "retired", reason: "能力下线" });
  const status = await j.status();
  assert.equal(status.trust, "retired_unconfirmed");
  assert.equal(status.active_release_id, null);
});

test("新版本激活覆盖旧活动版本", async () => {
  const j = journal();
  const old = await j.record({ ...base, kind: "activated" });
  await j.confirm(old.apply_id);
  const next = await j.record({
    ...base, release_id: "rel_9_2_0", version: "v9.2.0", kind: "activated",
  });
  await j.confirm(next.apply_id);
  const status = await j.status();
  assert.equal(status.trust, "installed_confirmed");
  assert.equal(status.active_release_id, "rel_9_2_0");
  assert.equal(status.active_version, "v9.2.0");
});

test("台账损坏按空台账恢复且不抛异常", async () => {
  const store = new MemoryStore();
  await store.setItem("install_journal_v1", "{ 不是 JSON");
  const j = journal(store);
  const status = await j.status();
  assert.equal(status.trust, "none");
});

test("schema 版本不符按空台账恢复", async () => {
  const store = new MemoryStore();
  await store.setItem("install_journal_v1", JSON.stringify({
    version: 999, entries: [{ kind: "activated" }],
  }));
  const j = journal(store);
  assert.deepEqual(await j.entries(), []);
});

test("缺字段条目被丢弃", async () => {
  const store = new MemoryStore();
  await store.setItem("install_journal_v1", JSON.stringify({
    version: 1,
    entries: [
      { kind: "activated", release_id: "r1", version: "v1",
        device_id: "d1", at: 1, apply_id: "apply_12345678" },
      { kind: "activated", release_id: "", version: "v1",
        device_id: "d1", at: 1, apply_id: "apply_12345678" },
      { kind: "bogus", release_id: "r1", version: "v1",
        device_id: "d1", at: 1, apply_id: "apply_12345678" },
    ],
  }));
  const j = journal(store);
  const entries = await j.entries();
  assert.equal(entries.length, 1);
  assert.equal(entries[0].release_id, "r1");
});

test("条目必须绑定设备", async () => {
  const j = journal();
  await assert.rejects(
    () => j.record({ ...base, device_id: "  ", kind: "activated" }),
    /设备/,
  );
});

test("confirm 对未知 apply_id 幂等无副作用", async () => {
  const j = journal();
  const entry = await j.record({ ...base, kind: "activated" });
  const missing = await j.confirm("apply_does_not_exist");
  assert.equal(missing, null);
  const status = await j.status();
  assert.equal(status.trust, "installed_unconfirmed");
  const again = await j.confirm(entry.apply_id);
  assert.equal(again?.apply_id, entry.apply_id);
  assert.equal((await j.status()).trust, "installed_confirmed");
});
