import { test } from "node:test";
import assert from "node:assert/strict";
import { generateKeyPairSync, sign } from "node:crypto";

import { MemoryStore } from "../../src/offline/storage.ts";
import { sha256Hex } from "../../src/offline/sha256.ts";
import { ArtifactDownloader } from "../../src/update/artifact.ts";
import type {
  ArtifactFileSystem, ArtifactSource,
} from "../../src/update/artifact.ts";
import { UpdateManager } from "../../src/update/manager.ts";
import type { BundleApplyRequest } from "../../src/update/manager.ts";
import { canonicalArtifactMessage, TrustedKeyRing } from "../../src/update/trust.ts";
import type { TrustedPublicKey } from "../../src/update/trust.ts";
import type {
  ApplyResultRequest, ReconcileRequest, ReconcileResponse,
  UpdateTarget, UpdateTransport,
} from "../../src/update/types.ts";

class FakeFs implements ArtifactFileSystem {
  files = new Map<string, Uint8Array>();
  free = 100 * 1024 * 1024;
  readonly removed: string[] = [];
  readonly renames: Array<[string, string]> = [];

  async stat(path: string) {
    const data = this.files.get(path);
    return data ? { size: data.length } : null;
  }

  async append(path: string, bytes: Uint8Array) {
    const current = this.files.get(path) ?? new Uint8Array();
    const next = new Uint8Array(current.length + bytes.length);
    next.set(current, 0);
    next.set(bytes, current.length);
    this.files.set(path, next);
  }

  async read(path: string) {
    const data = this.files.get(path);
    if (!data) throw new Error("missing file: " + path);
    return data;
  }

  async rename(from: string, to: string) {
    const data = this.files.get(from);
    if (!data) throw new Error("missing file: " + from);
    this.files.delete(from);
    this.files.set(to, data);
    this.renames.push([from, to]);
  }

  async remove(path: string) {
    this.files.delete(path);
    this.removed.push(path);
  }

  async freeBytes() {
    return this.free;
  }
}

class FakeSource implements ArtifactSource {
  body: Uint8Array;
  starts: number[] = [];
  truncateAt: number | null = null;
  constructor(body: Uint8Array) {
    this.body = body;
  }

  async fetchRange(_url: string, start: number, endInclusive: number) {
    this.starts.push(start);
    const limit = this.truncateAt ?? this.body.length;
    const end = Math.min(endInclusive + 1, limit);
    return {
      bytes: this.body.slice(Math.min(start, limit), end),
      totalBytes: this.body.length,
    };
  }
}

class FakeTransport implements UpdateTransport {
  response: ReconcileResponse | null = null;
  reports: ApplyResultRequest[] = [];
  reportError: Error | null = null;

  async reconcile(_req: ReconcileRequest) {
    return this.response as ReconcileResponse;
  }

  async candidateStates() {
    return {
      installation_id: "inst_1", current_release_id: null,
      candidates: [], server_time: 1,
    };
  }

  async applyResult(req: ApplyResultRequest) {
    this.reports.push(req);
    if (this.reportError) throw this.reportError;
    return { ok: true };
  }
}

function payload(): Uint8Array {
  return new TextEncoder().encode(
    JSON.stringify({ workflow: "demo/v2", tools: ["open_app"] }),
  );
}

function responseFor(target: UpdateTarget): ReconcileResponse {
  return {
    installation_id: "inst_1",
    decision: "update_available",
    update: {
      available: true, target_release_id: target.release_id,
      in_gray_scope: true, gray_percent: 100,
    },
    checks: [],
    reasons: [],
    current_release_id: null,
    target,
    server_time: 1700000000,
  };
}

function makeTargetWithArtifact(
  body: Uint8Array,
  overrides: Partial<UpdateTarget> = {},
  keys?: { publicKey: TrustedPublicKey; privateKey: unknown },
) {
  const digest = sha256Hex(body);
  const target: UpdateTarget = {
    release_id: "rel_art_1",
    version: "v9.1.0",
    artifact_digest: "sha256:" + digest,
    signed_by: "release-signer",
    signature_ok: true,
    rollback_target: "rel_9_0_0",
    min_app_version: "0.1.0",
    channel: "stable",
    bundle_kind: "workflow",
    delivery_mode: "capability_slot",
    gray_percent: 100,
    artifact_url: "https://cdn.example/artifacts/rel_art_1.bundle",
    artifact_size: body.length,
    artifact_sha256: digest,
    artifact_signature: "",
    signing_key_id: "release-key-1",
    signature_schema: "evoagent.release.signing/1",
    required_tool_contracts: [],
    required_permissions: [],
    ...overrides,
  };
  if (keys) {
    const message = canonicalArtifactMessage({
      schema: "evoagent.release.signing/1",
      release_id: target.release_id,
      version: target.version,
      artifact_sha256: target.artifact_sha256 as string,
      artifact_size: target.artifact_size as number,
      signing_key_id: target.signing_key_id as string,
      signature: "",
    });
    target.artifact_signature = sign(
      "sha256", Buffer.from(message), keys.privateKey as never,
    ).toString("hex");
  }
  return target;
}

function rsaKeys(): { publicKey: TrustedPublicKey; privateKey: unknown } {
  const pair = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const jwk = pair.publicKey.export({ format: "jwk" }) as {
    n: string; e: string;
  };
  return {
    publicKey: {
      key_id: "release-key-1", n: jwk.n, e: jwk.e, algorithm: "rsa-sha256",
    },
    privateKey: pair.privateKey,
  };
}

function setup(target: UpdateTarget, body: Uint8Array, keys: {
  publicKey: TrustedPublicKey; privateKey: unknown;
}, options: {
  fs?: FakeFs; source?: FakeSource; free?: number;
  applier?: { calls: BundleApplyRequest[]; fail?: boolean };
  permissions?: string[];
  appVersion?: string;
  now?: () => number;
  trustedKeys?: TrustedKeyRing;
} = {}) {
  const kv = new MemoryStore();
  const transport = new FakeTransport();
  transport.response = responseFor(target);
  const fs = options.fs ?? new FakeFs();
  const source = options.source ?? new FakeSource(body);
  const applier = options.applier;
  const manager = new UpdateManager(transport, kv, {
    appVersion: options.appVersion ?? "0.1.0",
    installationId: "inst_1",
    now: options.now ?? (() => 100),
    freeBytes: () => options.free ?? 100 * 1024 * 1024,
    toolContracts: ["open_app"],
    artifactSource: source,
    artifactFiles: fs,
    trustedKeys: options.trustedKeys
      ?? new TrustedKeyRing([keys.publicKey], options.now ?? (() => 100)),
    grantedPermissions: options.permissions ?? [],
    stagingDir: "updates",
    bundleApplier: applier
      ? {
        async apply(request: BundleApplyRequest) {
          applier.calls.push(request);
          if (applier.fail) throw new Error("apply failed");
        },
      }
      : undefined,
  });
  return { kv, transport, fs, source, manager };
}

test("downloader resumes from the staged offset and renames after digest check", async () => {
  const body = payload();
  const fs = new FakeFs();
  const source = new FakeSource(body);
  fs.files.set("updates/rel.part", body.slice(0, 10));
  const downloader = new ArtifactDownloader(source, fs);
  const result = await downloader.download({
    url: "https://cdn/x", sha256: sha256Hex(body), sizeBytes: body.length,
    stagingPath: "updates/rel.part", finalPath: "updates/rel.bundle",
    chunkSize: 8,
  });
  assert.equal(result.resumedFrom, 10);
  assert.equal(source.starts[0], 10);
  assert.equal(fs.files.has("updates/rel.part"), false);
  assert.deepEqual(fs.files.get("updates/rel.bundle"), body);
  assert.equal(result.sha256, sha256Hex(body));
});

test("downloader rejects bad digest and truncated sources without renaming", async () => {
  const body = payload();
  const fs = new FakeFs();
  const source = new FakeSource(body);
  const downloader = new ArtifactDownloader(source, fs);
  await assert.rejects(
    () => downloader.download({
      url: "https://cdn/x", sha256: "0".repeat(64), sizeBytes: body.length,
      stagingPath: "updates/rel.part", finalPath: "updates/rel.bundle",
      chunkSize: 8,
    }),
    /摘要不符/,
  );
  assert.equal(fs.files.has("updates/rel.bundle"), false);

  const truncated = new FakeSource(body);
  truncated.truncateAt = 20;
  const second = new ArtifactDownloader(truncated, fs);
  await assert.rejects(
    () => second.download({
      url: "https://cdn/x", sha256: sha256Hex(body), sizeBytes: body.length,
      stagingPath: "updates/rel2.part", finalPath: "updates/rel2.bundle",
      chunkSize: 8,
    }),
    /下载提前结束|下载未完成/,
  );
  assert.equal(fs.files.has("updates/rel2.bundle"), false);
});

test("downloader refuses when free space is below the next chunk", async () => {
  const body = payload();
  const fs = new FakeFs();
  fs.free = 1;
  const downloader = new ArtifactDownloader(new FakeSource(body), fs);
  await assert.rejects(
    () => downloader.download({
      url: "https://cdn/x", sha256: sha256Hex(body), sizeBytes: body.length,
      stagingPath: "updates/rel.part", finalPath: "updates/rel.bundle",
    }),
    /空间不足/,
  );
});

test("trusted key ring verifies real artifact bytes and rejects tampering", () => {
  const keys = rsaKeys();
  const body = payload();
  const target = makeTargetWithArtifact(body, {}, keys);
  const envelope = {
    schema: "evoagent.release.signing/1",
    release_id: target.release_id,
    version: target.version,
    artifact_sha256: target.artifact_sha256 as string,
    artifact_size: target.artifact_size as number,
    signing_key_id: target.signing_key_id as string,
    signature: target.artifact_signature as string,
  };
  const ring = new TrustedKeyRing([keys.publicKey], () => 100);
  assert.equal(ring.verifyArtifact(envelope, body).ok, true);

  const tampered = body.slice();
  tampered[0] ^= 0xff;
  const bad = ring.verifyArtifact(envelope, tampered);
  assert.equal(bad.ok, false);
  assert.match(bad.reasons.join(";"), /摘要与声明不符/);

  const unknown = ring.verifyArtifact({ ...envelope, signing_key_id: "ghost" }, body);
  assert.equal(unknown.ok, false);
  assert.match(unknown.reasons.join(";"), /未知签名密钥/);

  const revoked = new TrustedKeyRing(
    [{ ...keys.publicKey, revoked: true }], () => 100);
  assert.match(revoked.verifyArtifact(envelope, body).reasons.join(";"), /撤销/);

  const expired = new TrustedKeyRing(
    [{ ...keys.publicKey, not_after: 99 }], () => 100);
  assert.match(expired.verifyArtifact(envelope, body).reasons.join(";"), /过期/);
});

test("prepare downloads and verifies before allowing activation", async () => {
  const keys = rsaKeys();
  const body = payload();
  const target = makeTargetWithArtifact(body, {}, keys);
  const applier = { calls: [] as BundleApplyRequest[] };
  const ctx = setup(target, body, keys, { applier });

  await ctx.manager.check();
  const prepared = await ctx.manager.prepare();
  assert.equal(prepared.phase, "verified");
  assert.equal(prepared.artifact?.digest, sha256Hex(body));
  assert.equal(prepared.artifact?.signing_key_id, "release-key-1");
  assert.ok(ctx.fs.files.has("updates/rel_art_1.bundle"));
  const verified = ctx.transport.reports.at(-1);
  assert.equal(verified?.action, "verified");
  assert.equal(verified?.version, "v9.1.0");
  assert.equal(verified?.artifact_digest, sha256Hex(body));
  assert.equal(verified?.signing_key_id, "release-key-1");

  const active = await ctx.manager.activate();
  assert.equal(active.phase, "active");
  assert.equal(applier.calls.length, 1);
  assert.equal(applier.calls[0].slot, "b");
  assert.equal(applier.calls[0].digest, sha256Hex(body));
  assert.equal(active.slots.b?.signing_key_id, "release-key-1");
  assert.equal(active.artifact, null);
  const activated = ctx.transport.reports.at(-1);
  assert.equal(activated?.action, "activated");
  assert.equal(activated?.version, "v9.1.0");
});

test("prepare rejects an untrusted signing key before downloading", async () => {
  const keys = rsaKeys(), body = payload();
  const source = new FakeSource(body);
  const ctx = setup(makeTargetWithArtifact(body, {}, keys), body, keys, {
    source, trustedKeys: new TrustedKeyRing([]),
  });
  await ctx.manager.check();
  await assert.rejects(() => ctx.manager.prepare(), /未受信|失效/);
  assert.equal(source.starts.length, 0);
  assert.equal((await ctx.manager.status()).phase, "failed");
});

test("prepare blocks missing permissions, incompatible app and bad signature", async () => {
  const keys = rsaKeys();
  const body = payload();

  const permissionTarget = makeTargetWithArtifact(body, {
    required_permissions: ["camera"],
  }, keys);
  const denied = setup(permissionTarget, body, keys);
  await denied.manager.check();
  await assert.rejects(() => denied.manager.prepare(), /缺少权限/);
  assert.equal(denied.source.starts.length, 0);

  const oldApp = setup(makeTargetWithArtifact(body, {}, keys), body, keys,
    { appVersion: "0.0.9" });
  const blocked = await oldApp.manager.check();
  assert.equal(blocked.phase, "blocked");
  assert.match(blocked.last_error ?? "", /最低版本/);

  const badSignature = makeTargetWithArtifact(body, {}, keys);
  badSignature.artifact_signature = "00".repeat(256);
  const bad = setup(badSignature, body, keys);
  await bad.manager.check();
  await assert.rejects(() => bad.manager.prepare(), /验签失败/);
  assert.equal(bad.fs.files.has("updates/rel_art_1.bundle"), false);
  assert.equal(bad.manager.status instanceof Function, true);
  assert.equal((await bad.manager.status()).phase, "failed");
});

test("low free space and store failures never reach activation", async () => {
  const keys = rsaKeys();
  const body = payload();
  const target = makeTargetWithArtifact(body, {}, keys);
  const low = setup(target, body, keys, { free: 10 });
  await low.manager.check();
  await assert.rejects(() => low.manager.prepare(), /存储空间不足/);
  assert.equal(low.source.starts.length, 0);
  assert.equal((await low.manager.status()).active_slot, "a");
});

test("bundle apply failure keeps the active slot and reports activation_failed", async () => {
  const keys = rsaKeys();
  const body = payload();
  const target = makeTargetWithArtifact(body, {}, keys);
  const applier = { calls: [] as BundleApplyRequest[], fail: true };
  const ctx = setup(target, body, keys, { applier });
  await ctx.manager.check();
  await ctx.manager.prepare();
  await assert.rejects(() => ctx.manager.activate(), /装载失败/);
  const state = await ctx.manager.status();
  assert.equal(state.phase, "failed");
  assert.equal(state.active_slot, "a");
  assert.equal(state.slots.a, null);
  const receipt = ctx.transport.reports.at(-1);
  assert.equal(receipt?.action, "activation_failed");
  assert.equal(receipt?.artifact_digest, sha256Hex(body));
});

test("a restart before activation does not switch slots without a verifier/applier", async () => {
  const keys = rsaKeys();
  const body = payload();
  const target = makeTargetWithArtifact(body, {}, keys);
  const applier = { calls: [] as BundleApplyRequest[] };
  const ctx = setup(target, body, keys, { applier });
  await ctx.manager.check();
  await ctx.manager.prepare();

  const restarted = new UpdateManager(new FakeTransport(), ctx.kv, {
    appVersion: "0.1.0",
    installationId: "inst_1",
    freeBytes: () => 100 * 1024 * 1024,
    artifactFiles: ctx.fs,
    trustedKeys: new TrustedKeyRing([keys.publicKey]),
    stagingDir: "updates",
  });
  const state = await restarted.status();
  assert.equal(state.phase, "verified");
  assert.equal(state.active_slot, "a");
  await assert.rejects(() => restarted.activate(), /装载器/);
  assert.equal((await restarted.status()).active_slot, "a");
});

test("failed receipts persist and flush with the same apply id", async () => {
  const keys = rsaKeys();
  const body = payload();
  const target = makeTargetWithArtifact(body, {}, keys);
  const applier = { calls: [] as BundleApplyRequest[] };
  const ctx = setup(target, body, keys, { applier });
  await ctx.manager.check();
  ctx.transport.reportError = new Error("receipt down");
  await ctx.manager.prepare();
  assert.equal((await ctx.manager.status()).receipts.length, 1);
  const failed = ctx.transport.reports.at(-1);

  ctx.transport.reportError = null;
  const flushed = await ctx.manager.flushReceipts();
  assert.equal(flushed.receipts.length, 0);
  const replayed = ctx.transport.reports.at(-1);
  assert.equal(replayed?.apply_id, failed?.apply_id);
  assert.equal(replayed?.action, "verified");
});


// Independent acceptance: hostile inputs and state changes between prepare/apply.
test("real artifact cannot use the legacy verify shortcut", async () => {
  const keys = rsaKeys(), body = payload();
  const ctx = setup(makeTargetWithArtifact(body, {}, keys), body, keys);
  await ctx.manager.check();
  await assert.rejects(() => ctx.manager.verify(), /prepare|真实制品/);
  assert.equal((await ctx.manager.status()).active_slot, "a");
});

test("activation rechecks actual bytes before calling the applier", async () => {
  const keys = rsaKeys(), body = payload();
  const applier = { calls: [] as BundleApplyRequest[] };
  const ctx = setup(makeTargetWithArtifact(body, {}, keys), body, keys, { applier });
  await ctx.manager.check(); await ctx.manager.prepare();
  ctx.fs.files.set("updates/rel_art_1.bundle", new Uint8Array([42]));
  await assert.rejects(() => ctx.manager.activate(), /摘要|制品|校验/);
  assert.equal(applier.calls.length, 0);
  assert.equal((await ctx.manager.status()).active_slot, "a");
  const failed = ctx.transport.reports.filter(
    (receipt) => receipt.action === "activation_failed");
  assert.equal(failed.length, 1);
  assert.equal(failed[0].outcome, "failed");
});

test("activation rechecks permission revocation", async () => {
  const keys = rsaKeys(), body = payload(), permissions = ["camera"];
  const applier = { calls: [] as BundleApplyRequest[] };
  const ctx = setup(makeTargetWithArtifact(body, { required_permissions: ["camera"] }, keys),
    body, keys, { applier, permissions });
  await ctx.manager.check(); await ctx.manager.prepare();
  permissions.length = 0;
  await assert.rejects(() => ctx.manager.activate(), /权限/);
  assert.equal(applier.calls.length, 0);
});

test("activation rechecks signing key expiry", async () => {
  const keys = rsaKeys(), body = payload(); let now = 100;
  keys.publicKey.not_after = 200;
  const applier = { calls: [] as BundleApplyRequest[] };
  const ctx = setup(makeTargetWithArtifact(body, {}, keys), body, keys, { applier, now: () => now });
  await ctx.manager.check(); await ctx.manager.prepare(); now = 300;
  await assert.rejects(() => ctx.manager.activate(), /过期|验签/);
  assert.equal(applier.calls.length, 0);
});

test("unknown-length downloads enforce the byte cap before appending", async () => {
  const body = new Uint8Array(12).fill(7), fs = new FakeFs();
  const source: ArtifactSource = { async fetchRange(_url, start, end) {
    return { bytes: body.slice(start, end + 1) };
  } };
  await assert.rejects(() => new ArtifactDownloader(source, fs).download({
    url: "test", sha256: sha256Hex(body), stagingPath: "part", finalPath: "final",
    maxBytes: 8, chunkSize: 5,
  }), /超出/);
  assert.ok((fs.files.get("part")?.length ?? 0) <= 8);
  assert.equal(fs.files.has("final"), false);
});

test("cached files cannot evade the download byte cap", async () => {
  const body = new Uint8Array(12).fill(7), fs = new FakeFs();
  fs.files.set("final", body);
  await assert.rejects(() => new ArtifactDownloader(new FakeSource(body), fs).download({
    url: "test", sha256: sha256Hex(body), stagingPath: "part", finalPath: "final", maxBytes: 8,
  }), /超出/);
});

test("release IDs cannot escape the staging directory", async () => {
  const keys = rsaKeys(), body = payload();
  const ctx = setup(makeTargetWithArtifact(body, { release_id: "../outside" }, keys), body, keys);
  const state = await ctx.manager.check();
  assert.equal(state.phase, "blocked");
  assert.equal(ctx.source.starts.length, 0);
});

test("short App versions are compared with zero-filled segments", async () => {
  const keys = rsaKeys(), body = payload();
  const ctx = setup(makeTargetWithArtifact(body, { min_app_version: "1.1.0" }, keys), body, keys, { appVersion: "1" });
  assert.equal((await ctx.manager.check()).phase, "blocked");
});

test("real rollback reapplies the known-good bundle before reporting success", async () => {
  const keys = rsaKeys(), body = payload();
  const applier = { calls: [] as BundleApplyRequest[] };
  const ctx = setup(makeTargetWithArtifact(body, {}, keys), body, keys, { applier });
  await ctx.manager.check(); await ctx.manager.prepare(); await ctx.manager.activate();
  await ctx.manager.healthCheck(true);
  ctx.transport.response = responseFor(makeTargetWithArtifact(body,
    { release_id: "rel_art_2", version: "v9.2.0" }, keys));
  await ctx.manager.check(); await ctx.manager.prepare(); await ctx.manager.activate();
  const rolled = await ctx.manager.rollback();
  assert.equal(applier.calls.length, 3);
  assert.equal(applier.calls[2].release_id, "rel_art_1");
  assert.equal(applier.calls[2].slot, rolled.active_slot);
  assert.equal(ctx.transport.reports.at(-1)?.action, "rolled_back");
});

test("failed rollback apply preserves the active slot and does not report success", async () => {
  const keys = rsaKeys(), body = payload();
  const applier = { calls: [] as BundleApplyRequest[], fail: false };
  const ctx = setup(makeTargetWithArtifact(body, {}, keys), body, keys, { applier });
  await ctx.manager.check(); await ctx.manager.prepare(); await ctx.manager.activate();
  await ctx.manager.healthCheck(true);
  const before = await ctx.manager.status();
  applier.fail = true;
  await assert.rejects(() => ctx.manager.rollback(), /apply failed/);
  assert.equal((await ctx.manager.status()).active_slot, before.active_slot);
  assert.notEqual(ctx.transport.reports.at(-1)?.action, "rolled_back");
});

test("unacknowledged receipts are retained beyond fifty events", async () => {
  const keys = rsaKeys(), body = payload();
  const applier = { calls: [] as BundleApplyRequest[] };
  const ctx = setup(makeTargetWithArtifact(body, {}, keys), body, keys, { applier });
  await ctx.manager.check(); await ctx.manager.prepare(); await ctx.manager.activate();
  ctx.transport.reportError = new Error("offline");
  for (let index = 0; index < 51; index += 1) await ctx.manager.healthCheck(true);
  const reports = await ctx.manager.status();
  assert.equal(reports.receipts.length, 51);
  const firstId = reports.receipts[0].apply_id;
  ctx.transport.reportError = null;
  await ctx.manager.flushReceipts();
  assert.ok(ctx.transport.reports.some((r) => r.apply_id === firstId));
  assert.equal((await ctx.manager.status()).receipts.length, 0);
});

test("concurrent health checks cannot overwrite each other's receipts", async () => {
  const keys = rsaKeys(), body = payload();
  const applier = { calls: [] as BundleApplyRequest[] };
  const ctx = setup(makeTargetWithArtifact(body, {}, keys), body, keys, { applier });
  await ctx.manager.check(); await ctx.manager.prepare(); await ctx.manager.activate();
  ctx.transport.reportError = new Error("offline");
  await Promise.all(Array.from({ length: 4 }, () => ctx.manager.healthCheck(true)));
  assert.equal((await ctx.manager.status()).receipts.length, 4);
});

test("unconfirmed releases cannot replace the last known healthy bundle", async () => {
  const keys = rsaKeys(), body = payload();
  const applier = { calls: [] as BundleApplyRequest[] };
  const ctx = setup(makeTargetWithArtifact(body, {}, keys), body, keys, { applier });
  await ctx.manager.check(); await ctx.manager.prepare(); await ctx.manager.activate();
  await ctx.manager.healthCheck(true);
  for (const id of ["rel_art_2", "rel_art_3"]) {
    ctx.transport.response = responseFor(makeTargetWithArtifact(body, { release_id: id }, keys));
    await ctx.manager.check(); await ctx.manager.prepare(); await ctx.manager.activate();
  }
  assert.equal((await ctx.manager.status()).last_known_good?.release_id, "rel_art_1");
  await ctx.manager.healthCheck(false);
  assert.equal(applier.calls.at(-1)?.release_id, "rel_art_1");
});
