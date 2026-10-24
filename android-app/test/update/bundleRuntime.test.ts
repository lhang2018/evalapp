import { test } from "node:test";
import assert from "node:assert/strict";
import { generateKeyPairSync, sign } from "node:crypto";

import { MemoryStore } from "../../src/offline/storage.ts";
import type { KeyValueStore } from "../../src/offline/storage.ts";
import { sha256Hex } from "../../src/offline/sha256.ts";
import {
  WorkflowBundleApplier,
  loadActiveBundle,
  validateWorkflowDef,
  ACTIVE_BUNDLE_KEY,
  slotBundleKey,
} from "../../src/update/workflowBundle.ts";
import type { BundleWorkflowDef } from "../../src/update/workflowBundle.ts";
import {
  BundleRuntime,
  BundleRejectedError,
  versionAtLeast,
} from "../../src/update/bundleRuntime.ts";
import type { BundleTool } from "../../src/update/bundleRuntime.ts";
import { UpdateManager } from "../../src/update/manager.ts";
import type { ArtifactFileSystem, ArtifactSource } from "../../src/update/artifact.ts";
import { canonicalArtifactMessage, TrustedKeyRing } from "../../src/update/trust.ts";
import type { TrustedPublicKey } from "../../src/update/trust.ts";
import type {
  ApplyResultRequest, ReconcileRequest, ReconcileResponse,
  UpdateTarget, UpdateTransport,
} from "../../src/update/types.ts";

// ---- 夹具 ----

function timeDef(label: string, tool = "local_time"): BundleWorkflowDef {
  return {
    workflow_id: "time_report",
    version: "1.0.0",
    nodes: [{ node_id: "report", tool, args: { label } }],
  };
}

function bundleBody(fields: Record<string, unknown>): Uint8Array {
  return new TextEncoder().encode(JSON.stringify(fields));
}

function makeRuntime(
  kv: KeyValueStore,
  tools: Record<string, BundleTool>,
  options: {
    appVersion?: string;
    granted?: string[];
    executionTimeoutMs?: number;
  } = {},
): BundleRuntime {
  return new BundleRuntime(kv, {
    appVersion: options.appVersion ?? "0.1.0",
    tools,
    grantedPermissions: () => options.granted ?? [],
    executionTimeoutMs: options.executionTimeoutMs,
  });
}

async function installBundle(
  kv: KeyValueStore, fs: ArtifactFileSystem,
  fields: Record<string, unknown>,
  meta: { release_id: string; version: string; slot: "a" | "b" },
): Promise<string> {
  const body = bundleBody(fields);
  const digest = sha256Hex(body);
  const path = `updates/${meta.release_id}-${meta.version}.bundle`;
  await fs.append(path, body);
  const applier = new WorkflowBundleApplier(fs, kv);
  await applier.apply({ ...meta, path, digest });
  return digest;
}

/** 绕过装载器直接写槽位：模拟历史版本装载的坏包（装载校验加入之前）。 */
async function writeRawBundle(
  kv: KeyValueStore,
  fields: Record<string, unknown>,
  meta: { release_id: string; version: string; slot: "a" | "b" },
): Promise<void> {
  const digest = sha256Hex(bundleBody(fields));
  const record = {
    release_id: meta.release_id, version: meta.version, slot: meta.slot,
    digest, bundle_ref: null, installed_at: 1, bundle: fields,
  };
  await kv.setItem(slotBundleKey(meta.slot), JSON.stringify(record));
  await kv.setItem(ACTIVE_BUNDLE_KEY, JSON.stringify({
    slot: meta.slot, release_id: meta.release_id, version: meta.version,
    digest, installed_at: 1,
  }));
}

function baseTools(): Record<string, BundleTool> {
  return {
    local_time: async (args) => ({
      time: "2026-10-24 10:00:00",
      ...(typeof args.label === "string" ? { label: args.label } : {}),
    }),
    echo: async (args, ctx) => ({
      echoed: args.value ?? null,
      fromUpstream: ctx.outputs["a"] ?? null,
    }),
    boom: async () => {
      throw new Error("工具内部错误");
    },
  };
}

// ---- 更新管理器真实制品流夹具 ----

class FakeFs implements ArtifactFileSystem {
  files = new Map<string, Uint8Array>();
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
  }
  async remove(path: string) {
    this.files.delete(path);
  }
  async freeBytes() {
    return 100 * 1024 * 1024;
  }
}

class FakeSource implements ArtifactSource {
  bodies = new Map<string, Uint8Array>();
  async fetchRange(url: string, start: number, endInclusive: number) {
    const body = this.bodies.get(url);
    if (!body) throw new Error("no body for " + url);
    const end = Math.min(endInclusive + 1, body.length);
    return {
      bytes: body.slice(Math.min(start, body.length), end),
      totalBytes: body.length,
    };
  }
}

class FakeTransport implements UpdateTransport {
  response: ReconcileResponse;
  reports: ApplyResultRequest[] = [];
  constructor(response: ReconcileResponse) {
    this.response = response;
  }
  async reconcile(_req: ReconcileRequest) {
    return this.response;
  }
  async candidateStates() {
    return {
      installation_id: "inst_1", current_release_id: null,
      candidates: [], server_time: 1,
    };
  }
  async applyResult(req: ApplyResultRequest) {
    this.reports.push(req);
    return { ok: true };
  }
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

function makeTarget(
  fields: Record<string, unknown>, keys: ReturnType<typeof rsaKeys>,
): UpdateTarget {
  const body = fields.__body as Uint8Array;
  delete fields.__body;
  const digest = sha256Hex(body);
  const target: UpdateTarget = {
    release_id: "rel_x",
    version: "v1.0.0",
    artifact_digest: "sha256:" + digest,
    signed_by: "release-signer",
    signature_ok: true,
    rollback_target: null,
    min_app_version: "0.1.0",
    channel: "stable",
    bundle_kind: "workflow",
    delivery_mode: "capability_slot",
    gray_percent: 100,
    artifact_url: `https://cdn.example/${String(fields.release_id)}.bundle`,
    artifact_size: body.length,
    artifact_sha256: digest,
    artifact_signature: "",
    signing_key_id: "release-key-1",
    signature_schema: "evoagent.release.signing/1",
    required_tool_contracts: [],
    required_permissions: [],
    ...fields,
  };
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
  return target;
}

function reconcileFor(target: UpdateTarget): ReconcileResponse {
  return {
    installation_id: "inst_1",
    decision: "update_available",
    update: {
      available: true, target_release_id: target.release_id,
      in_gray_scope: true, gray_percent: 100,
    },
    checks: [], reasons: [],
    current_release_id: null, target, server_time: 1,
  };
}

// ---- 执行前拒绝 ----

test("没有活动包时执行前拒绝，工具不被调用", async () => {
  const kv = new MemoryStore();
  let called = 0;
  const runtime = makeRuntime(kv, {
    local_time: async () => {
      called += 1;
      return {};
    },
  });
  await assert.rejects(
    () => runtime.execute({ goal: "几点" }, "task-1"),
    (error: unknown) => {
      assert.ok(error instanceof BundleRejectedError);
      assert.equal(error.code, "no_active_bundle");
      return true;
    },
  );
  assert.equal(called, 0);
});

test("旧版包（未内嵌 workflow_def）不可执行，仅展示", async () => {
  const kv = new MemoryStore();
  const fs = new FakeFs();
  await installBundle(kv, fs, { workflow: "legacy" },
    { release_id: "rel_legacy", version: "1.0.0", slot: "a" });
  const runtime = makeRuntime(kv, baseTools());
  await assert.rejects(
    () => runtime.execute({}, "task-1"),
    (error: unknown) => {
      assert.ok(error instanceof BundleRejectedError);
      assert.equal(error.code, "not_executable");
      return true;
    },
  );
});

test("未注册工具在执行前拒绝", async () => {
  const kv = new MemoryStore();
  const fs = new FakeFs();
  await installBundle(kv, fs,
    { workflow: "time_report", workflow_def: timeDef("v1", "missing_tool") },
    { release_id: "rel_1", version: "1.0.0", slot: "a" });
  const runtime = makeRuntime(kv, baseTools());
  await assert.rejects(
    () => runtime.execute({}, "task-1"),
    (error: unknown) => {
      assert.ok(error instanceof BundleRejectedError);
      assert.equal(error.code, "tool_unregistered");
      assert.match(error.message, /missing_tool/);
      return true;
    },
  );
});

test("不兼容版本与权限不足在执行前拒绝", async () => {
  const kv = new MemoryStore();
  const fs = new FakeFs();
  await installBundle(kv, fs,
    { workflow: "time_report", min_app_version: "99.0.0",
      workflow_def: timeDef("v1") },
    { release_id: "rel_1", version: "1.0.0", slot: "a" });
  const runtime = makeRuntime(kv, baseTools());
  await assert.rejects(
    () => runtime.execute({}, "task-1"),
    (error: unknown) => {
      assert.ok(error instanceof BundleRejectedError);
      assert.equal(error.code, "incompatible");
      return true;
    },
  );

  const kv2 = new MemoryStore();
  await installBundle(kv2, fs,
    { workflow: "time_report", required_permissions: ["location"],
      workflow_def: timeDef("v1") },
    { release_id: "rel_2", version: "1.0.0", slot: "a" });
  const denied = makeRuntime(kv2, baseTools(), { granted: [] });
  await assert.rejects(
    () => denied.execute({}, "task-1"),
    (error: unknown) => {
      assert.ok(error instanceof BundleRejectedError);
      assert.equal(error.code, "permission_missing");
      return true;
    },
  );
  const granted = makeRuntime(kv2, baseTools(), { granted: ["location"] });
  const record = await granted.execute({}, "task-1");
  assert.equal(record.outcome, "succeeded");
});

test("非法 workflow（环/重复ID/未知依赖/标识不一致）在执行前拒绝", async () => {
  const kv = new MemoryStore();
  const cases: Array<[Record<string, unknown>, string]> = [
    [{ workflow: "time_report", workflow_def: {
      workflow_id: "time_report", version: "1.0.0",
      nodes: [
        { node_id: "a", tool: "local_time", deps: ["b"] },
        { node_id: "b", tool: "local_time", deps: ["a"] },
      ],
    } }, "依赖环"],
    [{ workflow: "time_report", workflow_def: {
      workflow_id: "time_report", version: "1.0.0",
      nodes: [
        { node_id: "a", tool: "local_time" },
        { node_id: "a", tool: "local_time" },
      ],
    } }, "节点ID重复"],
    [{ workflow: "time_report", workflow_def: {
      workflow_id: "time_report", version: "1.0.0",
      nodes: [{ node_id: "a", tool: "local_time", deps: ["ghost"] }],
    } }, "依赖不存在"],
    [{ workflow: "other_name", workflow_def: timeDef("v1") }, "标识不一致"],
  ];
  let index = 0;
  for (const [fields, label] of cases) {
    index += 1;
    const caseKv = new MemoryStore();
    // 直接写槽位：装载层校验会先拒绝这些包，这里验证运行时兜底
    await writeRawBundle(caseKv, fields,
      { release_id: `rel_bad_${index}`, version: "1.0.0", slot: "a" });
    const runtime = makeRuntime(caseKv, baseTools());
    await assert.rejects(
      () => runtime.execute({}, "task-1"),
      (error: unknown) => {
        assert.ok(error instanceof BundleRejectedError, label);
        assert.equal(error.code, "illegal_workflow", label);
        return true;
      },
      label,
    );
  }
});

test("装载层拒绝非法 workflow_def，非法包不会成为活动包", async () => {
  const kv = new MemoryStore();
  const fs = new FakeFs();
  await installBundle(kv, fs,
    { workflow: "time_report", workflow_def: timeDef("v0") },
    { release_id: "rel_ok", version: "1.0.0", slot: "a" });
  const applier = new WorkflowBundleApplier(fs, kv);
  const body = bundleBody({
    workflow: "time_report",
    workflow_def: {
      workflow_id: "time_report", version: "2.0.0",
      nodes: [
        { node_id: "a", tool: "local_time", deps: ["b"] },
        { node_id: "b", tool: "local_time", deps: ["a"] },
      ],
    },
  });
  await fs.append("updates/rel_bad.bundle", body);
  await assert.rejects(
    () => applier.apply({
      release_id: "rel_bad", version: "2.0.0",
      path: "updates/rel_bad.bundle", digest: sha256Hex(body), slot: "b",
    }),
    /workflow_def 非法/,
  );
  const active = await loadActiveBundle(kv);
  assert.equal(active?.release_id, "rel_ok");
});

// ---- 行为差异与版本绑定 ----

test("同一输入在更新前后使用各自版本 workflow，输出与证据绑定版本", async () => {
  const kv = new MemoryStore();
  const fs = new FakeFs();
  const runtime = makeRuntime(kv, baseTools());

  const digestV1 = await installBundle(kv, fs,
    { workflow: "time_report", intent: "time_query",
      workflow_def: timeDef("旧版输出") },
    { release_id: "rel_old", version: "1.0.0", slot: "a" });
  const r1 = await runtime.execute({ goal: "现在几点" }, "local_task_1");
  assert.equal(r1.outcome, "succeeded");
  assert.equal((r1.output as Record<string, unknown>).label, "旧版输出");
  assert.deepEqual(
    [r1.release_id, r1.version, r1.digest],
    ["rel_old", "1.0.0", digestV1]);
  assert.equal(r1.task_ref, "local_task_1");

  const digestV2 = await installBundle(kv, fs,
    { workflow: "time_report", intent: "time_query",
      workflow_def: { ...timeDef("新版输出"), version: "2.0.0" } },
    { release_id: "rel_new", version: "2.0.0", slot: "b" });
  const r2 = await runtime.execute({ goal: "现在几点" }, "local_task_2");
  assert.equal((r2.output as Record<string, unknown>).label, "新版输出");
  assert.deepEqual(
    [r2.release_id, r2.version, r2.digest],
    ["rel_new", "2.0.0", digestV2]);
  assert.notDeepEqual(r1.output, r2.output);

  const state = await runtime.loadState();
  assert.equal(state.records.length, 2);
  assert.equal(state.binding?.release_id, "rel_new");
  assert.ok(state.records.some(
    (record) => record.task_ref === "local_task_1"
      && record.version === "1.0.0"));
});

test("DAG 按依赖执行并传递上游输出；失败阻断下游并校验输出契约", async () => {
  const kv = new MemoryStore();
  const fs = new FakeFs();
  await installBundle(kv, fs,
    { workflow: "chain", workflow_def: {
      workflow_id: "chain", version: "1.0.0",
      nodes: [
        { node_id: "a", tool: "local_time", args: { label: "step-a" } },
        { node_id: "b", tool: "echo", deps: ["a"], args: { value: "x" } },
      ],
    } },
    { release_id: "rel_chain", version: "1.0.0", slot: "a" });
  const runtime = makeRuntime(kv, baseTools());
  const ok = await runtime.execute({}, "task-1");
  assert.deepEqual(ok.steps.map((step) => step.node_id), ["a", "b"]);
  const stepB = ok.steps.find((step) => step.node_id === "b");
  const upstream = (stepB?.output as Record<string, unknown>).fromUpstream;
  assert.equal((upstream as Record<string, unknown>).label, "step-a");
  assert.equal(ok.outcome, "succeeded");

  await installBundle(kv, fs,
    { workflow: "chain", workflow_def: {
      workflow_id: "chain", version: "1.1.0",
      nodes: [
        { node_id: "a", tool: "boom" },
        { node_id: "b", tool: "echo", deps: ["a"] },
      ],
    } },
    { release_id: "rel_chain", version: "1.1.0", slot: "b" });
  const failed = await runtime.execute({}, "task-2");
  assert.equal(failed.outcome, "failed");
  assert.equal(failed.steps.find((s) => s.node_id === "a")?.state, "failed");
  assert.match(failed.steps.find((s) => s.node_id === "b")?.error ?? "",
    /上游依赖未成功/);

  await installBundle(kv, fs,
    { workflow: "chain", workflow_def: {
      workflow_id: "chain", version: "1.2.0",
      nodes: [{ node_id: "a", tool: "local_time",
        output_schema: { type: "object", required: ["missing_field"] } }],
    } },
    { release_id: "rel_chain", version: "1.2.0", slot: "b" });
  const contract = await runtime.execute({}, "task-3");
  assert.equal(contract.outcome, "failed");
  assert.match(contract.steps.at(-1)?.error ?? "", /missing_field/);
});

// ---- 有界自动健康探针 ----

test("探针：执行成功且期望相符才算 healthy", async () => {
  const kv = new MemoryStore();
  const fs = new FakeFs();
  await installBundle(kv, fs,
    { workflow: "time_report", workflow_def: timeDef("v1-健康"),
      health_probe: { input: {}, expect_output_contains: "v1-健康" } },
    { release_id: "rel_h", version: "1.0.0", slot: "a" });
  const runtime = makeRuntime(kv, baseTools());
  const probe = await runtime.runHealthProbe();
  assert.equal(probe.outcome, "healthy");
  assert.equal(probe.version, "1.0.0");
  assert.ok(probe.execution_id);
  const state = await runtime.loadState();
  assert.equal(state.probes.length, 1);
  // 重复探针追加记录，不覆盖版本关联
  const again = await runtime.runHealthProbe();
  assert.equal(again.outcome, "healthy");
  assert.notEqual(again.probe_id, probe.probe_id);
  assert.equal((await runtime.loadState()).probes.length, 2);
});

test("探针：期望不符或执行失败为 unhealthy", async () => {
  const kv = new MemoryStore();
  const fs = new FakeFs();
  await installBundle(kv, fs,
    { workflow: "time_report", workflow_def: timeDef("v1"),
      health_probe: { input: {}, expect_output_contains: "不存在" } },
    { release_id: "rel_m", version: "1.0.0", slot: "a" });
  const runtime = makeRuntime(kv, baseTools());
  const mismatch = await runtime.runHealthProbe();
  assert.equal(mismatch.outcome, "unhealthy");
  assert.match(mismatch.reason, /期望不符/);

  await installBundle(kv, fs,
    { workflow: "time_report", workflow_def: timeDef("v2", "boom"),
      health_probe: { input: {} } },
    { release_id: "rel_m", version: "2.0.0", slot: "b" });
  const failed = await runtime.runHealthProbe();
  assert.equal(failed.outcome, "unhealthy");
  assert.match(failed.reason, /探针执行失败/);
  assert.equal(failed.version, "2.0.0");
});

test("探针：超时无结果为 unknown，不标健康", async () => {
  const kv = new MemoryStore();
  const fs = new FakeFs();
  await installBundle(kv, fs,
    { workflow: "time_report", workflow_def: timeDef("slow"),
      health_probe: { input: {} } },
    { release_id: "rel_slow", version: "1.0.0", slot: "a" });
  const runtime = makeRuntime(kv, {
    local_time: async () => {
      await new Promise((resolve) => setTimeout(resolve, 80));
      return {};
    },
  }, { executionTimeoutMs: 20 });
  const probe = await runtime.runHealthProbe();
  assert.equal(probe.outcome, "unknown");
  assert.notEqual(probe.outcome, "healthy");
  assert.match(probe.reason, /不标健康/);
});

test("探针：已激活但非法的包被判 unhealthy（执行前拒绝）", async () => {
  const kv = new MemoryStore();
  // 绕过装载器直接写入槽位：模拟历史版本装载的坏包
  await writeRawBundle(kv, {
    workflow: "time_report",
    workflow_def: {
      workflow_id: "time_report", version: "9.9.9",
      nodes: [
        { node_id: "a", tool: "local_time", deps: ["b"] },
        { node_id: "b", tool: "local_time", deps: ["a"] },
      ],
    },
    health_probe: { input: {} },
  }, { release_id: "rel_broken", version: "9.9.9", slot: "a" });
  const runtime = makeRuntime(kv, baseTools());
  const probe = await runtime.runHealthProbe();
  assert.equal(probe.outcome, "unhealthy");
  assert.match(probe.reason, /执行前拒绝\(illegal_workflow\)/);
});

test("declaresIntent 只匹配活动包声明的意图", async () => {
  const kv = new MemoryStore();
  const fs = new FakeFs();
  const runtime = makeRuntime(kv, baseTools());
  assert.equal(await runtime.declaresIntent("time_query"), false);
  await installBundle(kv, fs,
    { workflow: "time_report", intent: "time_query",
      workflow_def: timeDef("v1") },
    { release_id: "rel_i", version: "1.0.0", slot: "a" });
  assert.equal(await runtime.declaresIntent("time_query"), true);
  assert.equal(await runtime.declaresIntent("general"), false);
});

// ---- 健康失败 → 真实回滚 → 原包恢复执行 ----

test("健康失败自动回滚后，原健康包真实可执行且版本关联跨重启保留", async () => {
  const kv = new MemoryStore();
  const fs = new FakeFs();
  const keys = rsaKeys();
  const now = () => 100;
  const oldBody = bundleBody({
    workflow: "time_report", workflow_def: timeDef("旧版"),
    health_probe: { input: {}, expect_output_contains: "旧版" },
  });
  const newBody = bundleBody({
    workflow: "time_report", workflow_def: timeDef("新版"),
    health_probe: { input: {}, expect_output_contains: "新版" },
  });
  const oldTarget = makeTarget({
    __body: oldBody, release_id: "rel_old", version: "v1.0.0",
  }, keys);
  const newTarget = makeTarget({
    __body: newBody, release_id: "rel_new", version: "v2.0.0",
  }, keys);
  const source = new FakeSource();
  source.bodies.set(oldTarget.artifact_url as string, oldBody);
  source.bodies.set(newTarget.artifact_url as string, newBody);
  const transport = new FakeTransport(reconcileFor(oldTarget));
  const manager = new UpdateManager(transport, kv, {
    appVersion: "0.1.0", installationId: "inst_1",
    now,
    toolContracts: [],
    artifactSource: source,
    artifactFiles: fs,
    trustedKeys: new TrustedKeyRing([keys.publicKey], now),
    bundleApplier: new WorkflowBundleApplier(fs, kv),
    grantedPermissions: () => [],
    stagingDir: "updates",
  });
  const runtime = makeRuntime(kv, baseTools());

  // 1) 旧版本激活并确认健康
  await manager.check();
  await manager.prepare();
  await manager.activate();
  const healthy = await runtime.runHealthProbe("activate");
  assert.equal(healthy.outcome, "healthy");
  await manager.healthCheck(true);
  const afterOld = await manager.status();
  assert.equal(afterOld.last_known_good?.release_id, "rel_old");

  // 2) 新版本激活；探针期望相符为 healthy（此时尚未晋级 last_known_good）
  transport.response = reconcileFor(newTarget);
  await manager.check();
  await manager.prepare();
  await manager.activate();
  const active1 = await manager.status();
  assert.equal(active1.slots[active1.active_slot]?.release_id, "rel_new");
  const probe = await runtime.runHealthProbe("activate");
  assert.equal(probe.outcome, "healthy");

  // 3) 坏版本（内嵌节点引用失败工具）激活；探针执行失败 → unhealthy
  const badBody = bundleBody({
    workflow: "time_report", workflow_def: timeDef("坏版本", "boom"),
    health_probe: { input: {} },
  });
  const badTarget = makeTarget({
    __body: badBody, release_id: "rel_bad", version: "v3.0.0",
  }, keys);
  source.bodies.set(badTarget.artifact_url as string, badBody);
  transport.response = reconcileFor(badTarget);
  await manager.check();
  await manager.prepare();
  await manager.activate();
  const badProbe = await runtime.runHealthProbe("activate");
  assert.equal(badProbe.outcome, "unhealthy");
  assert.equal(badProbe.version, "v3.0.0");

  // 4) 健康检查失败 → 管理器自动回滚到 last_known_good
  await manager.healthCheck(false);
  const rolled = await manager.status();
  assert.equal(rolled.phase, "rolled_back");
  assert.equal(rolled.slots[rolled.active_slot]?.release_id, "rel_old");
  const active = await loadActiveBundle(kv);
  assert.equal(active?.release_id, "rel_old");
  assert.ok(transport.reports.some(
    (report) => report.action === "rolled_back"
      && report.release_id === "rel_old"
      && report.to_release_id === "rel_old"));

  // 4) 恢复验证：原健康包用同一探针输入真实执行成功
  const recovery = await runtime.verifyRecovery(badProbe.reason);
  assert.equal(recovery.outcome, "succeeded");
  assert.equal(recovery.failed?.version, "v3.0.0");
  assert.equal(recovery.recovered?.release_id, "rel_old");

  // 5) 重启（新实例、同一 KV）后版本关联不丢失；重复确认不覆盖历史
  const restarted = makeRuntime(kv, baseTools());
  const persisted = await restarted.loadState();
  assert.ok(persisted.probes.some(
    (p) => p.outcome === "unhealthy" && p.version === "v3.0.0"));
  assert.equal(persisted.recovery?.recovered?.release_id, "rel_old");
  const postRestart = await restarted.runHealthProbe("manual");
  assert.equal(postRestart.outcome, "healthy");
  assert.equal(postRestart.version, "v1.0.0");
  const finalState = await restarted.loadState();
  assert.ok(finalState.probes.some(
    (p) => p.version === "v3.0.0" && p.outcome === "unhealthy"));
  assert.equal(finalState.recovery?.failed?.version, "v3.0.0");
});

// ---- 版本比较 ----

test("versionAtLeast 语义与更新管理器一致", () => {
  assert.equal(versionAtLeast("0.1.0", "0.1.0"), true);
  assert.equal(versionAtLeast("0.2.0", "0.1.0"), true);
  assert.equal(versionAtLeast("0.1.0", "0.2.0"), false);
  assert.equal(versionAtLeast("v1.0", "1.0.0"), true);
  assert.equal(versionAtLeast("abc", "0.1.0"), false);
});

test("validateWorkflowDef 拒绝无可执行体与非法参数", () => {
  assert.ok(validateWorkflowDef({
    workflow_id: "w", version: "1", nodes: [{ node_id: "a", tool: "" }],
  }).length > 0);
  assert.ok(validateWorkflowDef({
    workflow_id: "w", version: "1",
    nodes: [{ node_id: "a", tool: "t", args: "not-object" }],
  }).length > 0);
  assert.deepEqual(validateWorkflowDef({
    workflow_id: "w", version: "1",
    nodes: [{ node_id: "a", tool: "t" }],
  }), []);
});
