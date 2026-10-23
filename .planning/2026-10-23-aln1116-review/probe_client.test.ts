import { test } from "node:test";
import assert from "node:assert/strict";

import { sha256Hex } from "../../src/offline/sha256.ts";
import { ArtifactDownloadError, ArtifactDownloader } from "../../src/update/artifact.ts";
import type { ArtifactFileSystem, ArtifactSource, RangeResponse } from "../../src/update/artifact.ts";
import { releaseKeysForBuild } from "../../src/update/devTrust.ts";

// 独立探针：不复用被审方测试，只断言「修复后的正确行为」。

class MemFs implements ArtifactFileSystem {
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

/** 总长未知、无限制吐字节的源 */
function endlessSource(chunk: number): ArtifactSource {
  return {
    async fetchRange(): Promise<RangeResponse> {
      return { bytes: new Uint8Array(chunk).fill(7) };
    },
  };
}

test("CR11-08 production build must not trust the bundled development key", () => {
  assert.equal(releaseKeysForBuild(false).length, 0);
  assert.equal(releaseKeysForBuild(true).length, 1);
});

test("CR11-05 unknown total download must stop at the byte cap", async () => {
  const fs = new MemFs();
  const downloader = new ArtifactDownloader(endlessSource(64), fs);
  await assert.rejects(
    () => downloader.download({
      url: "https://example.invalid/a.bin",
      sha256: "a".repeat(64),
      stagingPath: "staging/a.part",
      finalPath: "staging/a.bundle",
      maxBytes: 128,
      chunkSize: 64,
    }),
    (error: unknown) => {
      assert.ok(error instanceof ArtifactDownloadError);
      assert.equal((error as ArtifactDownloadError).code, "too_large");
      return true;
    },
  );
});

test("CR11-05 cached final file must not evade the byte cap", async () => {
  const fs = new MemFs();
  const cached = new Uint8Array(256).fill(3);
  fs.files.set("staging/a.bundle", cached);
  const downloader = new ArtifactDownloader(endlessSource(64), fs);
  await assert.rejects(
    () => downloader.download({
      url: "https://example.invalid/a.bin",
      sha256: sha256Hex(cached),
      stagingPath: "staging/a.part",
      finalPath: "staging/a.bundle",
      maxBytes: 128,
      chunkSize: 64,
    }),
    (error: unknown) => {
      assert.ok(error instanceof ArtifactDownloadError);
      assert.equal((error as ArtifactDownloadError).code, "too_large");
      return true;
    },
  );
});

test("CR11-05 resumed staging file must not evade the byte cap", async () => {
  const fs = new MemFs();
  fs.files.set("staging/a.part", new Uint8Array(256).fill(5));
  const downloader = new ArtifactDownloader(endlessSource(64), fs);
  await assert.rejects(
    () => downloader.download({
      url: "https://example.invalid/a.bin",
      sha256: "b".repeat(64),
      stagingPath: "staging/a.part",
      finalPath: "staging/a.bundle",
      maxBytes: 128,
      chunkSize: 64,
    }),
    (error: unknown) => {
      assert.ok(error instanceof ArtifactDownloadError);
      assert.equal((error as ArtifactDownloadError).code, "too_large");
      return true;
    },
  );
});

test("CR11-05 illegal limits are rejected before any transfer", async () => {
  const fs = new MemFs();
  const downloader = new ArtifactDownloader(endlessSource(64), fs);
  for (const options of [
    { maxBytes: 0, chunkSize: 64 },
    { maxBytes: 128, chunkSize: 0 },
    { maxBytes: 128, chunkSize: 1.5 },
    { maxBytes: 128, chunkSize: 64, sizeBytes: 999 },
  ]) {
    await assert.rejects(
      () => downloader.download({
        url: "https://example.invalid/a.bin",
        sha256: "c".repeat(64),
        stagingPath: "staging/a.part",
        finalPath: "staging/a.bundle",
        ...options,
      }),
      (error: unknown) => {
        assert.ok(error instanceof ArtifactDownloadError);
        assert.ok(["bad_limit", "too_large"].includes((error as ArtifactDownloadError).code));
        return true;
      },
    );
  }
});
