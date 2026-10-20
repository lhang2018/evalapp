import { test } from "node:test";
import assert from "node:assert/strict";

import { MemoryStore } from "../../src/offline/storage.ts";
import { UpdateManager } from "../../src/update/manager.ts";
import type {
  ApplyResultRequest,
  CandidateState,
  CandidateSyncResponse,
  ReconcileRequest,
  ReconcileResponse,
  UpdateTarget,
  UpdateTransport,
} from "../../src/update/types.ts";

function target(overrides: Partial<UpdateTarget> = {}): UpdateTarget {
  return {
    release_id: "rel_9_1_0",
    version: "v9.1.0",
    artifact_digest: "sha256:abcdef0123456789",
    signed_by: "evoagent-release",
    signature_ok: true,
    rollback_target: "rel_9_0_0",
    min_app_version: "0.1.0",
    channel: "stable",
    bundle_kind: "capability",
    delivery_mode: "capability_slot",
    gray_percent: 100,
    created_at: 1700000000,
    ...overrides,
  };
}

function reconcileResponse(t: UpdateTarget | null): ReconcileResponse {
  return {
    installation_id: "inst_test_0001",
    decision: t ? "update_available" : "up_to_date",
    update: {
      available: Boolean(t),
      target_release_id: t?.release_id ?? null,
      in_gray_scope: true,
      gray_percent: 100,
    },
    checks: [],
    reasons: [],
    current_release_id: null,
    target: t,
    server_time: 1700000000,
  };
}

class FakeTransport implements UpdateTransport {
  response: ReconcileResponse = reconcileResponse(null);
  candidateResponse: CandidateSyncResponse = {
    installation_id: "inst_test_0001",
    current_release_id: null,
    candidates: [],
    server_time: 1700000000,
  };
  reconcileCalls: ReconcileRequest[] = [];
  candidateCalls: string[] = [];
  reports: ApplyResultRequest[] = [];
  reconcileError: Error | null = null;
  candidateError: Error | null = null;
  reportError: Error | null = null;

  async reconcile(req: ReconcileRequest): Promise<ReconcileResponse> {
    this.reconcileCalls.push(req);
    if (this.reconcileError) throw this.reconcileError;
    return this.response;
  }

  async candidateStates(id: string): Promise<CandidateSyncResponse> {
    this.candidateCalls.push(id);
    if (this.candidateError) throw this.candidateError;
    return this.candidateResponse;
  }

  async applyResult(req: ApplyResultRequest) {
    this.reports.push(req);
    if (this.reportError) throw this.reportError;
    return { ok: true };
  }
}

function setup(options: {
  now?: () => number;
  freeBytes?: () => number;
  storeKey?: string;
} = {}) {
  const kv = new MemoryStore();
  const transport = new FakeTransport();
  const manager = new UpdateManager(transport, kv, {
    appVersion: "0.1.0",
    installationId: "inst_test_0001",
    now: options.now,
    freeBytes: options.freeBytes,
    storeKey: options.storeKey,
  });
  return { kv, transport, manager };
}

async function readyManager(
  t: UpdateTarget,
  options: { freeBytes?: () => number; now?: () => number } = {},
) {
  const ctx = setup(options);
  ctx.transport.response = reconcileResponse(t);
  await ctx.manager.check();
  return ctx;
}

test("init generates and persists an installation id", async () => {
  const kv = new MemoryStore();
  const first = new UpdateManager(new FakeTransport(), kv, {
    appVersion: "0.1.0",
  });
  const state = await first.init();
  assert.match(state.installation_id, /^inst_/);
  assert.equal(state.active_slot, "a");
  assert.equal(state.phase, "idle");

  const second = new UpdateManager(new FakeTransport(), kv, {
    appVersion: "0.1.0",
  });
  const again = await second.init();
  assert.equal(again.installation_id, state.installation_id);
});

test("check accepts a signed capability target", async () => {
  const { manager, transport } = setup();
  transport.response = reconcileResponse(target());
  const state = await manager.check();
  assert.equal(state.phase, "available");
  assert.equal(state.target?.release_id, "rel_9_1_0");
  assert.equal(transport.reconcileCalls[0].installation_id, "inst_test_0001");
  assert.equal(transport.reconcileCalls[0].app_version, "0.1.0");
});

test("check blocks target without artifact digest", async () => {
  const { manager, transport } = setup();
  transport.response = reconcileResponse(target({ artifact_digest: null }));
  const state = await manager.check();
  assert.equal(state.phase, "blocked");
  assert.equal(state.target, null);
  assert.match(state.last_error ?? "", /摘要/);
});

test("check blocks unsigned target", async () => {
  const { manager, transport } = setup();
  transport.response = reconcileResponse(target({ signature_ok: false }));
  const state = await manager.check();
  assert.equal(state.phase, "blocked");
  assert.match(state.last_error ?? "", /签名/);
});

test("check blocks native binary updates that need the app store", async () => {
  const { manager, transport } = setup();
  transport.response = reconcileResponse(target({ delivery_mode: "client_binary" }));
  const state = await manager.check();
  assert.equal(state.phase, "blocked");
  assert.match(state.last_error ?? "", /二进制更新/);
});

test("network failure during check marks phase failed and rethrows", async () => {
  const { manager, transport } = setup();
  transport.reconcileError = new Error("offline");
  await assert.rejects(() => manager.check(), /offline/);
  const state = await manager.status();
  assert.equal(state.phase, "failed");
  assert.match(state.last_error ?? "", /offline/);
});

test("verify refuses when storage headroom is insufficient", async () => {
  const { manager } = await readyManager(target(), { freeBytes: () => 1024 });
  await assert.rejects(() => manager.verify(10 * 1024 * 1024), /存储空间不足/);
  const state = await manager.status();
  assert.equal(state.phase, "failed");
  assert.equal(state.pending, null);
});

test("verify stores a pending candidate after integrity and space checks", async () => {
  const { manager, transport } = await readyManager(target(), {
    freeBytes: () => 100 * 1024 * 1024,
  });
  const state = await manager.verify(8 * 1024 * 1024);
  assert.equal(state.phase, "verified");
  assert.equal(state.pending?.release_id, "rel_9_1_0");
  assert.equal(transport.reports.at(-1)?.action, "verified");
});

test("activate requires a verified candidate", async () => {
  const { manager } = await readyManager(target());
  await assert.rejects(() => manager.activate(), /尚未通过校验/);
});

test("verify without a candidate throws", async () => {
  const { manager } = setup();
  await assert.rejects(() => manager.verify(), /没有可验证的候选版本/);
});

test("activate switches the inactive slot and keeps the previous release", async () => {
  const now = 1000;
  const { manager, transport } = await readyManager(target(), {
    freeBytes: () => Number.MAX_SAFE_INTEGER,
    now: () => now,
  });
  await manager.verify();
  const state = await manager.activate();
  assert.equal(state.phase, "active");
  assert.equal(state.active_slot, "b");
  assert.equal(state.slots.b?.release_id, "rel_9_1_0");
  assert.equal(state.slots.b?.activated_at, 1000);
  assert.equal(state.pending, null);
  const actions = transport.reports.map((r) => r.action);
  assert.deepEqual(actions, ["verified", "activation_pending", "activated"]);
});

test("health check success promotes the active slot to last known good", async () => {
  const { manager } = await readyManager(target(), {
    freeBytes: () => Number.MAX_SAFE_INTEGER,
  });
  await manager.verify();
  await manager.activate();
  const state = await manager.healthCheck(true);
  assert.equal(state.phase, "active");
  assert.equal(state.last_known_good?.release_id, "rel_9_1_0");
});

test("health check failure rolls back to the last known good release", async () => {
  const { manager, transport } = setup({
    freeBytes: () => Number.MAX_SAFE_INTEGER,
  });

  transport.response = reconcileResponse(target({ release_id: "rel_9_0_0", version: "v9.0.0" }));
  await manager.check();
  await manager.verify();
  await manager.activate();
  await manager.healthCheck(true);

  transport.response = reconcileResponse(target({ release_id: "rel_9_1_0", version: "v9.1.0" }));
  await manager.check();
  await manager.verify();
  await manager.activate();
  assert.equal((await manager.status()).slots.a?.release_id, "rel_9_1_0");

  const state = await manager.healthCheck(false);
  assert.equal(state.phase, "rolled_back");
  assert.equal(state.active_slot, "b");
  assert.equal(state.slots.b?.release_id, "rel_9_0_0");
  const last = transport.reports.at(-1);
  assert.equal(last?.action, "rolled_back");
  assert.equal(last?.to_release_id, "rel_9_0_0");
  assert.match(last?.reason ?? "", /健康检查失败/);
});

test("rollback without a known good release throws", async () => {
  const { manager } = await readyManager(target(), {
    freeBytes: () => Number.MAX_SAFE_INTEGER,
  });
  await manager.verify();
  await manager.activate();
  await assert.rejects(() => manager.rollback(), /没有可回滚的健康版本/);
});

test("report upload failure keeps the local activation and records the error", async () => {
  const { manager, transport } = await readyManager(target(), {
    freeBytes: () => Number.MAX_SAFE_INTEGER,
  });
  await manager.verify();
  transport.reportError = new Error("receipt down");
  const state = await manager.activate();
  assert.equal(state.phase, "active");
  assert.equal(state.active_slot, "b");
  const persisted = await manager.status();
  assert.match(persisted.last_error ?? "", /回执上传失败/);
});

test("corrupted state falls back to an empty slot state", async () => {
  const kv = new MemoryStore();
  await kv.setItem("update_state_v1", "{not json");
  const manager = new UpdateManager(new FakeTransport(), kv, {
    appVersion: "0.1.0",
    installationId: "inst_test_0001",
  });
  const state = await manager.init();
  assert.equal(state.phase, "idle");
  assert.equal(state.slots.a, null);
  assert.equal(state.installation_id, "inst_test_0001");
});

test("candidate sync stores server stages without changing the active slot", async () => {
  const { manager, transport } = await readyManager(target(), {
    freeBytes: () => Number.MAX_SAFE_INTEGER,
    now: () => 2000,
  });
  await manager.verify();
  await manager.activate();
  const candidate: CandidateState = {
    candidate_id: "cand_001",
    status: "MONITORING",
    target_component: "workflow/demo",
    target_domain: "general",
    risk_level: "L2",
    publish_suggestion: "release_ready",
    release_id: "rel_cand_001",
    created_at: 1500,
    updated_at: 1900,
  };
  transport.candidateResponse = {
    installation_id: "inst_test_0001",
    current_release_id: "rel_9_1_0",
    target_release_id: "rel_9_1_0",
    candidates: [candidate],
    server_time: 2000,
  };
  const state = await manager.refreshCandidates();
  assert.deepEqual(transport.candidateCalls, ["inst_test_0001"]);
  assert.equal(state.active_slot, "b");
  assert.equal(state.candidates.length, 1);
  assert.equal(state.candidates[0].candidate_id, "cand_001");
  assert.equal(state.candidates_synced_at, 2000);
  const persisted = await manager.status();
  assert.equal(persisted.candidates[0].status, "MONITORING");
});

test("candidate sync failure records an error and keeps local state", async () => {
  const { manager, transport } = await readyManager(target(), {
    freeBytes: () => Number.MAX_SAFE_INTEGER,
  });
  await manager.verify();
  const before = await manager.activate();
  transport.candidateError = new Error("offline");
  await assert.rejects(
    () => manager.refreshCandidates(),
    /offline/,
  );
  const after = await manager.status();
  assert.equal(after.phase, before.phase);
  assert.equal(after.active_slot, before.active_slot);
  assert.match(after.last_error ?? "", /候选状态同步失败/);
});

test("state persisted before candidate sync defaults to an empty list", async () => {
  const kv = new MemoryStore();
  await kv.setItem("update_state_v1", JSON.stringify({
    version: 1,
    installation_id: "inst_test_0001",
    active_slot: "a",
    slots: { a: null, b: null },
    last_known_good: null,
    pending: null,
    phase: "idle",
    target: null,
    checks: [],
    reasons: [],
    last_checked_at: null,
    last_error: null,
  }));
  const manager = new UpdateManager(new FakeTransport(), kv, {
    appVersion: "0.1.0",
    installationId: "inst_test_0001",
  });
  const state = await manager.status();
  assert.deepEqual(state.candidates, []);
  assert.equal(state.candidates_synced_at, null);
});
