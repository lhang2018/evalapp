/**
 * 诊断附件上传（补充 C §6）：显式授权、分块断点续传、全量 SHA-256 校验。
 *
 * 只持久化元数据与分块进度；原始字节保存在内存中，进程重启后需要重新选择
 * 同一文件（按 SHA-256/大小复用原记录，服务端已收到的分块继续复用）。附件
 * 可先于事件上传，事件只带 attachment_refs。
 */

import type { OfflineRepository } from "./storage.ts";
import type { LocalAttachment, OfflineState } from "./types.ts";
import { createId } from "./ids.ts";
import { sha256Hex } from "./sha256.ts";

export const ATTACHMENT_MAX_BYTES = 50 * 1024 * 1024;
export const ATTACHMENT_CHUNK_BYTES = 256 * 1024;
export const MAX_LOCAL_ATTACHMENTS = 20;
export const MAX_ATTACHMENT_REFS = 8;

/** 服务端返回的附件元数据（结构兼容 EvoAgentClient 的 AttachmentInfo）。 */
export interface RemoteAttachment {
  attachment_id: string;
  file_name?: string;
  content_type?: string;
  size_bytes: number;
  sha256: string;
  chunk_size: number;
  chunk_count: number;
  status: string;
  received_bytes: number;
  received_chunks: number[];
}

export interface AttachmentInitPayload {
  attachment_id: string;
  file_name: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  chunk_size: number;
  chunk_count: number;
  consent: boolean;
}

/** 传输契约；网络错误直接抛出，由调用方决定是否重试。 */
export interface AttachmentTransport {
  initAttachment(info: AttachmentInitPayload): Promise<RemoteAttachment>;
  uploadAttachmentChunk(
    attachmentId: string,
    index: number,
    bytes: Uint8Array,
  ): Promise<{
    received_bytes: number;
    received_chunks: number[];
    chunk_sha256: string;
  }>;
  completeAttachment(attachmentId: string): Promise<{
    attachment: RemoteAttachment;
    verified: boolean;
    duplicate?: boolean;
  }>;
}

function chunkSlice(
  bytes: Uint8Array,
  index: number,
  chunkSize: number,
): Uint8Array {
  const start = index * chunkSize;
  return bytes.subarray(start, Math.min(start + chunkSize, bytes.length));
}

function mergeChunks(current: number[], incoming: number[]): number[] {
  const out = new Set(current);
  for (const index of incoming) {
    if (Number.isInteger(index) && index >= 0) out.add(index);
  }
  return [...out].sort((a, b) => a - b);
}

function updateAttachment(
  state: OfflineState,
  attachmentId: string,
  patch: Partial<LocalAttachment>,
  now: number,
): void {
  const item = state.attachments.find(
    (entry) => entry.attachment_id === attachmentId,
  );
  if (!item) return;
  Object.assign(item, patch, { updated_at: now });
}

export class AttachmentService {
  private readonly repo: OfflineRepository;
  private readonly transport: AttachmentTransport;
  private readonly now: () => number;
  /** 原始字节只驻留内存；重启后为空，需重新选择同一文件以复用记录。 */
  private readonly sources = new Map<string, Uint8Array>();

  constructor(
    repo: OfflineRepository,
    transport: AttachmentTransport,
    now: () => number = () => Date.now(),
  ) {
    this.repo = repo;
    this.transport = transport;
    this.now = now;
  }

  /**
   * 登记一个待上传附件。consent 必须显式为 true（高敏感材料），否则拒绝。
   * 字节不落盘，只记录文件名、大小、摘要与分块参数。
   */
  async add(
    fileName: string,
    contentType: string,
    bytes: Uint8Array,
    consent: boolean = false,
  ): Promise<LocalAttachment> {
    if (!consent) {
      throw new Error("上传诊断附件需要显式授权（consent=true）");
    }
    if (bytes.length === 0) {
      throw new Error("附件不能为空");
    }
    if (bytes.length > ATTACHMENT_MAX_BYTES) {
      throw new Error("附件超过 50 MiB 上限");
    }
    const sha = sha256Hex(bytes);
    const chunkSize = ATTACHMENT_CHUNK_BYTES;
    const now = this.now();
    const item = await this.repo.update((state) => {
      // 相同内容复用既有记录：重启后重新选择同一文件可继续未完成的分块
      const existing = state.attachments.find(
        (entry) =>
          entry.sha256 === sha && entry.size_bytes === bytes.length,
      );
      if (existing) {
        if (existing.status !== "complete") {
          existing.status = "pending";
          existing.last_error = null;
        }
        existing.file_name = fileName || existing.file_name;
        existing.updated_at = now;
        return { ...existing };
      }
      const created: LocalAttachment = {
        attachment_id: createId("att"),
        file_name: fileName || "attachment.bin",
        content_type: contentType || "application/octet-stream",
        size_bytes: bytes.length,
        sha256: sha,
        chunk_size: chunkSize,
        chunk_count: Math.ceil(bytes.length / chunkSize),
        uploaded_chunks: [],
        status: "pending",
        consent: true,
        created_at: now,
        updated_at: now,
        completed_at: null,
        last_error: null,
      };
      state.attachments.push(created);
      if (state.attachments.length > MAX_LOCAL_ATTACHMENTS) {
        state.attachments = state.attachments.slice(-MAX_LOCAL_ATTACHMENTS);
      }
      return created;
    });
    if (item.status !== "complete") {
      this.sources.set(item.attachment_id, bytes);
    }
    return item;
  }

  /** 上传所有未完成附件；返回本轮完成的条数。单条失败不影响其余条目。 */
  async flush(): Promise<number> {
    const state = await this.repo.load();
    const targets = state.attachments.filter(
      (item) => item.status !== "complete",
    );
    let completed = 0;
    for (const item of targets) {
      const bytes = this.sources.get(item.attachment_id);
      if (!bytes) {
        await this.markFailed(
          item.attachment_id,
          "原始字节不在内存中（进程重启后需重新选择文件）",
        );
        continue;
      }
      try {
        await this.uploadOne(item, bytes);
        completed += 1;
      } catch (error) {
        // 服务端摘要校验失败（422）说明已传分块不可信，清空本地进度后重传
        if ((error as { status?: number })?.status === 422) {
          await this.repo.update((state) => {
            updateAttachment(
              state,
              item.attachment_id,
              { uploaded_chunks: [] },
              this.now(),
            );
          });
        }
        await this.markFailed(
          item.attachment_id,
          error instanceof Error ? error.message : String(error),
        );
      }
    }
    return completed;
  }

  private async uploadOne(
    item: LocalAttachment,
    bytes: Uint8Array,
  ): Promise<void> {
    const remote = await this.transport.initAttachment({
      attachment_id: item.attachment_id,
      file_name: item.file_name,
      content_type: item.content_type,
      size_bytes: item.size_bytes,
      sha256: item.sha256,
      chunk_size: item.chunk_size,
      chunk_count: item.chunk_count,
      consent: item.consent,
    });
    const remoteDone = new Set(remote.received_chunks ?? []);
    const localDone = new Set(item.uploaded_chunks);
    await this.repo.update((state) => {
      updateAttachment(
        state,
        item.attachment_id,
        {
          status: "uploading",
          uploaded_chunks: mergeChunks([...localDone], [...remoteDone]),
          last_error: null,
        },
        this.now(),
      );
    });

    for (let index = 0; index < item.chunk_count; index += 1) {
      if (remoteDone.has(index) || localDone.has(index)) continue;
      const chunk = chunkSlice(bytes, index, item.chunk_size);
      const expected = sha256Hex(chunk);
      const result = await this.transport.uploadAttachmentChunk(
        item.attachment_id,
        index,
        chunk,
      );
      if (result.chunk_sha256 && result.chunk_sha256 !== expected) {
        throw new Error(`分块 ${index} 摘要不一致`);
      }
      localDone.add(index);
      const reported = result.received_chunks ?? [index];
      for (const value of reported) localDone.add(value);
      await this.repo.update((state) => {
        updateAttachment(
          state,
          item.attachment_id,
          {
            uploaded_chunks: [...localDone].sort((a, b) => a - b),
          },
          this.now(),
        );
      });
    }

    const done = await this.transport.completeAttachment(item.attachment_id);
    if (!done.verified || done.attachment.status !== "complete") {
      await this.repo.update((state) => {
        updateAttachment(
          state,
          item.attachment_id,
          { uploaded_chunks: [] },
          this.now(),
        );
      });
      throw new Error("服务端未确认附件完整性");
    }
    await this.repo.update((state) => {
      updateAttachment(
        state,
        item.attachment_id,
        {
          status: "complete",
          uploaded_chunks: Array.from(
            { length: item.chunk_count },
            (_, index) => index,
          ),
          completed_at: this.now(),
          last_error: null,
        },
        this.now(),
      );
    });
  }

  private async markFailed(attachmentId: string, message: string) {
    await this.repo.update((state) => {
      updateAttachment(
        state,
        attachmentId,
        { status: "failed", last_error: message },
        this.now(),
      );
    });
  }

  async list(): Promise<LocalAttachment[]> {
    const state = await this.repo.load();
    return [...state.attachments];
  }

  /** 已完成附件引用，供事件 attachment_refs 使用（最多 8 项）。 */
  async refsFor(attachmentIds?: string[]): Promise<string[]> {
    const state = await this.repo.load();
    const wanted = attachmentIds ? new Set(attachmentIds) : null;
    return state.attachments
      .filter((item) =>
        item.status === "complete" && (!wanted || wanted.has(item.attachment_id))
      )
      .map((item) => item.attachment_id)
      .slice(0, MAX_ATTACHMENT_REFS);
  }
}
