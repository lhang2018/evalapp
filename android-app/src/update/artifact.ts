/**
 * 端侧制品断点下载与暂存（ALN-11）。
 *
 * 只信任真实字节：下载写入 `.part`，完整后校验摘要再原子改名为正式制品。
 * 网络、文件与空间能力全部通过端口注入，便于真机适配与 Node 测试。
 */

import { sha256Hex } from "../offline/sha256.ts";
import type { KeyValueStore } from "../offline/storage.ts";

export interface RangeResponse {
  bytes: Uint8Array;
  totalBytes?: number;
  etag?: string;
}

export interface ArtifactSource {
  fetchRange(
    url: string,
    start: number,
    endInclusive: number,
    etag?: string,
  ): Promise<RangeResponse>;
}

export interface ArtifactFileSystem {
  stat(path: string): Promise<{ size: number } | null>;
  append(path: string, bytes: Uint8Array): Promise<void>;
  read(path: string): Promise<Uint8Array>;
  rename(from: string, to: string): Promise<void>;
  remove(path: string): Promise<void>;
  freeBytes(): Promise<number>;
}

export interface ArtifactDownloadOptions {
  url: string;
  sha256: string;
  sizeBytes?: number;
  stagingPath: string;
  finalPath: string;
  maxBytes?: number;
  chunkSize?: number;
  checkpoint?: KeyValueStore;
  checkpointKey?: string;
  onProgress?: (info: { written: number; total: number | null }) => void;
}

export interface ArtifactResult {
  path: string;
  size: number;
  sha256: string;
  resumedFrom: number;
}

export class ArtifactDownloadError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.code = code;
    this.name = "ArtifactDownloadError";
  }
}

const DEFAULT_CHUNK = 256 * 1024;
const DEFAULT_MAX = 64 * 1024 * 1024;

function normalizeDigest(value: string): string {
  return value.trim().toLowerCase().replace(/^sha256:/, "");
}

export class ArtifactDownloader {
  private readonly source: ArtifactSource;
  private readonly fs: ArtifactFileSystem;

  constructor(source: ArtifactSource, fs: ArtifactFileSystem) {
    this.source = source;
    this.fs = fs;
  }

  async download(options: ArtifactDownloadOptions): Promise<ArtifactResult> {
    const expected = normalizeDigest(options.sha256);
    if (!/^[0-9a-f]{64}$/.test(expected)) {
      throw new ArtifactDownloadError("bad_digest", "缺少合法的 SHA-256 摘要");
    }
    const maxBytes = options.maxBytes ?? DEFAULT_MAX;
    const chunkSize = options.chunkSize ?? DEFAULT_CHUNK;
    if (options.sizeBytes !== undefined
        && (options.sizeBytes < 0 || options.sizeBytes > maxBytes)) {
      throw new ArtifactDownloadError("too_large", "制品大小超出允许范围");
    }

    const existingFinal = await this.fs.stat(options.finalPath);
    if (existingFinal) {
      const bytes = await this.fs.read(options.finalPath);
      if (sha256Hex(bytes) === expected
          && (options.sizeBytes === undefined
            || bytes.length === options.sizeBytes)) {
        return {
          path: options.finalPath,
          size: bytes.length,
          sha256: expected,
          resumedFrom: bytes.length,
        };
      }
      await this.fs.remove(options.finalPath);
    }

    let offset = (await this.fs.stat(options.stagingPath))?.size ?? 0;
    if (options.sizeBytes !== undefined && offset > options.sizeBytes) {
      await this.fs.remove(options.stagingPath);
      offset = 0;
    }
    const resumedFrom = offset;
    const checkpointKey = options.checkpointKey ?? "update_download_v1";
    let total = options.sizeBytes ?? 0;
    let etag: string | undefined;
    if (options.checkpoint) {
      try {
        const raw = await options.checkpoint.getItem(checkpointKey);
        if (raw) {
          const saved = JSON.parse(raw) as {
            url?: string; sha256?: string; total?: number; etag?: string;
          };
          if (saved.url === options.url
              && saved.sha256 === expected && offset > 0) {
            total = total || Number(saved.total) || 0;
            etag = saved.etag;
          }
        }
      } catch {
        // 检查点损坏时按全新下载处理，暂存文件仍由摘要兜底
      }
    }

    while (total === 0 || offset < total) {
      const end = offset + chunkSize - 1;
      const response = await this.source.fetchRange(
        options.url, offset, end, etag);
      const bytes = response.bytes;
      if (!bytes.length) {
        throw new ArtifactDownloadError(
          "short_read", `下载提前结束: ${offset}/${total || "?"}`);
      }
      if (!total && response.totalBytes !== undefined) {
        total = response.totalBytes;
      }
      if (total > maxBytes) {
        throw new ArtifactDownloadError("too_large", "制品大小超出允许范围");
      }
      const next = offset + bytes.length;
      if (total && next > total) {
        throw new ArtifactDownloadError("size_mismatch", "制品实际大小不符");
      }
      const free = await this.fs.freeBytes();
      if (free < bytes.length) {
        throw new ArtifactDownloadError("no_space", "存储空间不足");
      }
      await this.fs.append(options.stagingPath, bytes);
      offset = next;
      etag = response.etag ?? etag;
      if (options.checkpoint) {
        await options.checkpoint.setItem(checkpointKey, JSON.stringify({
          url: options.url, sha256: expected, total, etag,
          written: offset, updated_at: Date.now(),
        }));
      }
      options.onProgress?.({ written: offset, total: total || null });
      if (!total && bytes.length < chunkSize) break;
    }

    if (total && offset !== total) {
      throw new ArtifactDownloadError(
        "short_read", `下载未完成: ${offset}/${total}`);
    }
    const bytes = await this.fs.read(options.stagingPath);
    const actual = sha256Hex(bytes);
    if (actual !== expected
        || (options.sizeBytes !== undefined
          && bytes.length !== options.sizeBytes)) {
      await this.fs.remove(options.stagingPath);
      throw new ArtifactDownloadError("digest_mismatch", "制品摘要不符");
    }
    await this.fs.rename(options.stagingPath, options.finalPath);
    if (options.checkpoint) {
      await options.checkpoint.removeItem(checkpointKey);
    }
    return {
      path: options.finalPath,
      size: bytes.length,
      sha256: actual,
      resumedFrom,
    };
  }
}
