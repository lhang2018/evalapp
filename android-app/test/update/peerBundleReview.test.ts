import { test } from "node:test";
import assert from "node:assert/strict";
import { MemoryStore } from "../../src/offline/storage.ts";
import { ACTIVE_BUNDLE_KEY, slotBundleKey, loadActiveBundle } from "../../src/update/workflowBundle.ts";
import { BundleRuntime } from "../../src/update/bundleRuntime.ts";
import { UpdateManager, UPDATE_STATE_KEY } from "../../src/update/manager.ts";

async function seed(kv: MemoryStore, version = "1", probe: Record<string, unknown> = {}) {
  const row = { slot: "a", release_id: "release-" + version, version, digest: "digest-" + version, installed_at: 1,
    bundle: { workflow: "review", workflow_def: { workflow_id: "review", version, nodes: [{ node_id: "first", tool: "tool", max_retries: 2 },
      { node_id: "after", tool: "after", deps: ["first"] }] }, health_probe: probe } };
  await kv.setItem(slotBundleKey("a"), JSON.stringify(row));
  await kv.setItem(ACTIVE_BUNDLE_KEY, JSON.stringify({ ...row, bundle: undefined }));
}
function runtime(kv: MemoryStore, tool: (...args: any[]) => Promise<any>, after = async () => "done") {
  return new BundleRuntime(kv, { appVersion: "1.0.0", tools: { tool, after }, grantedPermissions: () => [], executionTimeoutMs: 25 });
}
test("normal execution has a hard wait bound and never dispatches a dependent after a late timeout", async () => {
  const kv = new MemoryStore(); await seed(kv); let calls = 0, dependent = 0;
  let complete!: (value: unknown) => void;
  const rt = runtime(kv, async () => { calls++; return new Promise(r => { complete = r; }); }, async () => { dependent++; return "done"; });
  let timer: ReturnType<typeof setTimeout>;
  try {
    const result = await Promise.race([rt.execute({}, "task"), new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error("unbounded execution")), 250); })]);
    assert.equal(result.outcome, "timeout");
    complete("late"); await new Promise(r => setTimeout(r, 30));
    assert.equal(calls, 1); assert.equal(dependent, 0);
    assert.equal((await rt.loadState()).records.at(-1)?.outcome, "timeout");
  } finally { clearTimeout(timer!); complete?.("cleanup"); }
});
test("a health result cannot certify an active bundle switched during the probe", async () => {
  const kv = new MemoryStore(); await seed(kv);
  const rt = runtime(kv, async () => { await seed(kv, "2"); return "ok"; });
  assert.equal((await rt.runHealthProbe()).outcome, "unknown");
});
test("recovery must satisfy the restored bundle expectation", async () => {
  const kv = new MemoryStore(); await seed(kv, "new", { expect_output_equals: "expected" });
  const rt = runtime(kv, async () => "wrong", async () => "wrong");
  await rt.runHealthProbe(); await seed(kv, "old", { expect_output_equals: "expected" });
  assert.equal((await rt.verifyRecovery("rollback")).outcome, "failed");
});
test("recovery cannot call the same failed release a rollback", async () => {
  const kv = new MemoryStore(); await seed(kv, "new", { expect_output_equals: "expected" });
  const rt = runtime(kv, async () => "wrong", async () => "wrong");
  await rt.runHealthProbe();
  assert.notEqual((await rt.verifyRecovery("not actually switched")).outcome, "succeeded");
});
test("active marker and slot contents must refer to the same artifact", async () => {
  const kv = new MemoryStore(); await seed(kv);
  const marker = JSON.parse((await kv.getItem(ACTIVE_BUNDLE_KEY))!); marker.digest = "different";
  await kv.setItem(ACTIVE_BUNDLE_KEY, JSON.stringify(marker));
  assert.equal(await loadActiveBundle(kv), null);
});

test("update manager validates the probe artifact inside its serialized health decision", async () => {
  const kv = new MemoryStore();
  const manager = new UpdateManager({ applyResult: async () => { throw new Error("must not report"); } } as any, kv,
    { appVersion: "1.0.0", installationId: "review" });
  const state = await manager.status();
  state.phase = "active";
  state.slots.a = { release_id: "current", version: "2", digest: "current-digest", activated_at: 1 };
  await kv.setItem(UPDATE_STATE_KEY, JSON.stringify(state));
  await assert.rejects(manager.healthCheck(true, { release_id: "old", version: "1", digest: "old-digest" }), /活动版本已变化/);
  assert.equal((await manager.status()).last_known_good, null);
});
