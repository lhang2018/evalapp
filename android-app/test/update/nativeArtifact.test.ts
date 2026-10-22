import { test } from "node:test";
import assert from "node:assert/strict";

import { base64ToBytes, bytesToBase64, utf8Decode } from "../../src/update/base64.ts";
import {
  ACTIVE_BUNDLE_KEY,
  loadActiveBundle,
  slotBundleKey,
  WorkflowBundleApplier,
} from "../../src/update/workflowBundle.ts";
import { FetchArtifactSource } from "../../src/update/rangeSource.ts";
import type { ArtifactFileSystem } from "../../src/update/artifact.ts";
import { MemoryStore } from "../../src/offline/storage.ts";
import { sha256Hex } from "../../src/offline/sha256.ts";
import { releaseKeysForBuild } from "../../src/update/devTrust.ts";
import { TrustedKeyRing } from "../../src/update/trust.ts";

class MemoryFs implements ArtifactFileSystem {
  files = new Map<string, Uint8Array>();
  free = 1024 * 1024;

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
    if (!data) throw new Error("missing: " + path);
    return data;
  }
  async rename(from: string, to: string) {
    const data = this.files.get(from);
    if (!data) throw new Error("missing: " + from);
    this.files.delete(from);
    this.files.set(to, data);
  }
  async remove(path: string) {
    this.files.delete(path);
  }
  async freeBytes() {
    return this.free;
  }
}

test("base64 codec round-trips arbitrary bytes and UTF-8 text", () => {
  const bytes = new Uint8Array(257);
  for (let index = 0; index < bytes.length; index += 1) {
    bytes[index] = (index * 37) & 0xff;
  }
  assert.deepEqual(base64ToBytes(bytesToBase64(bytes)), bytes);
  assert.equal(bytesToBase64(new Uint8Array()), "");

  const text = "能力包 workflow/v2 — ünïcode";
  const encoded = new TextEncoder().encode(text);
  assert.equal(utf8Decode(encoded), text);
});

test("production trust never accepts the bundled development key", () => {
  const envelope = {
    schema: "evoagent.release.signing/1", release_id: "rel", version: "v1",
    artifact_sha256: sha256Hex(new Uint8Array([1])), artifact_size: 1,
    signing_key_id: "dev-release-1", signature: "00",
  };
  assert.match(new TrustedKeyRing(releaseKeysForBuild(false))
    .verifyArtifact(envelope, new Uint8Array([1])).reasons.join(";"), /未知签名密钥/);
  assert.doesNotMatch(new TrustedKeyRing(releaseKeysForBuild(true))
    .verifyArtifact(envelope, new Uint8Array([1])).reasons.join(";"), /未知签名密钥/);
});

test("bundle applier installs to the slot and points the active marker", async () => {
  const fs = new MemoryFs();
  const kv = new MemoryStore();
  const body = new TextEncoder().encode(
    JSON.stringify({ workflow: "demo/v2", steps: [{ tool: "open_app" }] }),
  );
  fs.files.set("updates/rel.bundle", body);
  const applier = new WorkflowBundleApplier(fs, kv, () => 123);
  await applier.apply({
    release_id: "rel_1", version: "v9.1.0", path: "updates/rel.bundle",
    digest: sha256Hex(body), slot: "b", bundle_ref: "bundle://rel_1",
  });

  const slot = JSON.parse((await kv.getItem(slotBundleKey("b"))) as string);
  assert.equal(slot.release_id, "rel_1");
  assert.equal(slot.bundle.workflow, "demo/v2");
  assert.equal(slot.installed_at, 123);

  const active = JSON.parse((await kv.getItem(ACTIVE_BUNDLE_KEY)) as string);
  assert.equal(active.slot, "b");
  const restored = await loadActiveBundle(kv);
  assert.equal(restored?.release_id, "rel_1");
  assert.equal(restored?.slot, "b");
  assert.equal(restored?.bundle.workflow, "demo/v2");
});

test("bundle applier checks the bytes it reads against the verified digest", async () => {
  const fs = new MemoryFs(), kv = new MemoryStore();
  const original = new TextEncoder().encode(JSON.stringify({ workflow: "safe" }));
  fs.files.set("updates/rel.bundle", new TextEncoder().encode(JSON.stringify({ workflow: "changed" })));
  await assert.rejects(() => new WorkflowBundleApplier(fs, kv).apply({
    release_id: "rel", version: "v1", path: "updates/rel.bundle",
    digest: sha256Hex(original), slot: "b",
  }), /摘要/);
  assert.equal(await kv.getItem(ACTIVE_BUNDLE_KEY), null);
});

test("bundle applier rejects invalid payloads without touching the marker", async () => {
  const fs = new MemoryFs();
  const kv = new MemoryStore();
  const applier = new WorkflowBundleApplier(fs, kv);

  for (const body of ["not json", "[]", JSON.stringify({ tools: [] })]) {
    fs.files.set("updates/bad.bundle", new TextEncoder().encode(body));
    await assert.rejects(() => applier.apply({
      release_id: "rel_bad", version: "v1", path: "updates/bad.bundle",
      digest: "b".repeat(64), slot: "a",
    }));
    assert.equal(await kv.getItem(ACTIVE_BUNDLE_KEY), null);
    assert.equal(await kv.getItem(slotBundleKey("a")), null);
  }
  assert.equal(await loadActiveBundle(kv), null);
});

class StubXhr {
  static next: {
    status: number; body: ArrayBuffer; headers: Record<string, string>;
  } | null = null;
  static lastHeaders: Record<string, string> = {};
  status = 0;
  response: ArrayBuffer | null = null;
  timeout = 0;
  responseType = "";
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  ontimeout: (() => void) | null = null;
  private headers: Record<string, string> = {};
  private url = "";

  open(_method: string, url: string) {
    this.url = url;
  }
  setRequestHeader(name: string, value: string) {
    this.headers[name.toLowerCase()] = value;
  }
  getResponseHeader(name: string) {
    return StubXhr.next?.headers[name.toLowerCase()] ?? null;
  }
  send() {
    StubXhr.lastHeaders = { ...this.headers, url: this.url };
    const stub = StubXhr.next;
    if (!stub) {
      this.onerror?.();
      return;
    }
    this.status = stub.status;
    this.response = stub.body;
    setTimeout(() => this.onload?.(), 0);
  }
}

test("range source slices full responses and parses totals", async () => {
  const original = (globalThis as Record<string, unknown>).XMLHttpRequest;
  (globalThis as Record<string, unknown>).XMLHttpRequest = StubXhr;
  try {
    const payload = new TextEncoder().encode("0123456789abcdef");
    StubXhr.next = {
      status: 206,
      body: payload.slice(4, 12).buffer,
      headers: {
        "content-range": "bytes 4-11/16",
        etag: '"v1"',
      },
    };
    const source = new FetchArtifactSource();
    const ranged = await source.fetchRange("https://cdn/x", 4, 11);
    assert.equal(StubXhr.lastHeaders.range, "bytes=4-11");
    assert.equal(ranged.totalBytes, 16);
    assert.equal(ranged.etag, '"v1"');
    assert.deepEqual(ranged.bytes, payload.slice(4, 12));

    StubXhr.next = {
      status: 200,
      body: payload.buffer.slice(0),
      headers: {},
    };
    const full = await source.fetchRange("https://cdn/x", 4, 11);
    assert.equal(full.totalBytes, 16);
    assert.deepEqual(full.bytes, payload.slice(4));
  } finally {
    (globalThis as Record<string, unknown>).XMLHttpRequest = original;
  }
});
