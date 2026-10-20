import { test } from "node:test";
import assert from "node:assert/strict";

import { MemoryStore, OfflineRepository } from "../../src/offline/storage.ts";
import { EventQueue } from "../../src/offline/queue.ts";
import {
  ATTACHMENT_CHUNK_BYTES,
  AttachmentService,
} from "../../src/offline/attachments.ts";
import type {
  AttachmentInitPayload,
  AttachmentTransport,
  RemoteAttachment,
} from "../../src/offline/attachments.ts";
import { sha256Hex } from "../../src/offline/sha256.ts";

class FakeAttachmentTransport implements AttachmentTransport {
  initCalls: AttachmentInitPayload[] = [];
  chunkCalls: {
    attachmentId: string;
    index: number;
    bytes: Uint8Array;
  }[] = [];
  completeCalls: string[] = [];
  failNextChunk = false;
  badNextChunkSha = false;
  forceCompleteMismatch = false;
  throwOnComplete: number | null = null;
  private chunks = new Map<string, Map<number, Uint8Array>>();
  private declared = new Map<string, AttachmentInitPayload>();

  private store(id: string): Map<number, Uint8Array> {
    let map = this.chunks.get(id);
    if (!map) {
      map = new Map();
      this.chunks.set(id, map);
    }
    return map;
  }

  seed(id: string, index: number, bytes: Uint8Array): void {
    this.store(id).set(index, bytes.slice());
  }

  private progress(id: string): {
    received_bytes: number;
    received_chunks: number[];
  } {
    const map = this.store(id);
    const indexes = [...map.keys()].sort((a, b) => a - b);
    return {
      received_bytes: indexes.reduce(
        (total, index) => total + (map.get(index)?.length ?? 0),
        0,
      ),
      received_chunks: indexes,
    };
  }

  async initAttachment(info: AttachmentInitPayload): Promise<RemoteAttachment> {
    this.initCalls.push(info);
    this.declared.set(info.attachment_id, info);
    const progress = this.progress(info.attachment_id);
    return {
      attachment_id: info.attachment_id,
      size_bytes: info.size_bytes,
      sha256: info.sha256,
      chunk_size: info.chunk_size,
      chunk_count: info.chunk_count,
      status: "uploading",
      ...progress,
    };
  }

  async uploadAttachmentChunk(
    attachmentId: string,
    index: number,
    bytes: Uint8Array,
  ) {
    this.chunkCalls.push({ attachmentId, index, bytes });
    if (this.failNextChunk) {
      this.failNextChunk = false;
      throw new Error("network down");
    }
    const stored = bytes.slice();
    this.store(attachmentId).set(index, stored);
    const chunkSha = this.badNextChunkSha ? "0".repeat(64) : sha256Hex(stored);
    this.badNextChunkSha = false;
    return {
      ...this.progress(attachmentId),
      chunk_sha256: chunkSha,
    };
  }

  async completeAttachment(attachmentId: string) {
    this.completeCalls.push(attachmentId);
    if (this.throwOnComplete) {
      const status = this.throwOnComplete;
      this.throwOnComplete = null;
      throw Object.assign(new Error("附件摘要不一致"), { status });
    }
    const info = this.declared.get(attachmentId);
    if (!info) throw new Error("unknown attachment");
    const map = this.store(attachmentId);
    const parts: Uint8Array[] = [];
    for (let index = 0; index < info.chunk_count; index += 1) {
      const part = map.get(index);
      if (!part) {
        return {
          attachment: {
            attachment_id: attachmentId,
            size_bytes: info.size_bytes,
            sha256: info.sha256,
            chunk_size: info.chunk_size,
            chunk_count: info.chunk_count,
            status: "uploading",
            ...this.progress(attachmentId),
          },
          verified: false,
          duplicate: false,
        };
      }
      parts.push(part);
    }
    const total = new Uint8Array(
      parts.reduce((size, part) => size + part.length, 0),
    );
    let offset = 0;
    for (const part of parts) {
      total.set(part, offset);
      offset += part.length;
    }
    const verified = !this.forceCompleteMismatch && total.length === info.size_bytes &&
      sha256Hex(total) === info.sha256;
    if (!verified) {
      // 与服务端一致：摘要不一致时丢弃全部分块
      this.chunks.set(attachmentId, new Map());
    }
    return {
      attachment: {
        attachment_id: attachmentId,
        size_bytes: info.size_bytes,
        sha256: info.sha256,
        chunk_size: info.chunk_size,
        chunk_count: info.chunk_count,
        status: verified ? "complete" : "uploading",
        ...this.progress(attachmentId),
      },
      verified,
      duplicate: false,
    };
  }
}

function payloadOf(size: number): Uint8Array {
  const bytes = new Uint8Array(size);
  for (let index = 0; index < size; index += 1) bytes[index] = index % 251;
  return bytes;
}

function setup() {
  const repo = new OfflineRepository(new MemoryStore());
  const transport = new FakeAttachmentTransport();
  let clock = 1_700_000_000_000;
  const service = new AttachmentService(repo, transport, () => {
    clock += 1;
    return clock;
  });
  return { repo, transport, service };
}

test("add requires explicit consent and non-empty bytes", async () => {
  const { repo, service } = setup();
  await assert.rejects(
    service.add("screen.png", "image/png", payloadOf(10), false),
    /显式授权/,
  );
  await assert.rejects(
    service.add("screen.png", "image/png", new Uint8Array(0), true),
    /不能为空/,
  );
  assert.equal((await repo.load()).attachments.length, 0);
});

test("add records digest, chunk plan and pending status", async () => {
  const { repo, service } = setup();
  const bytes = payloadOf(ATTACHMENT_CHUNK_BYTES + 10);
  const item = await service.add("screen.png", "image/png", bytes, true);
  assert.equal(item.size_bytes, bytes.length);
  assert.equal(item.sha256, sha256Hex(bytes));
  assert.equal(item.chunk_count, 2);
  assert.equal(item.status, "pending");
  assert.deepEqual(item.uploaded_chunks, []);
  assert.equal(item.consent, true);

  const stored = await service.list();
  assert.equal(stored.length, 1);
  assert.equal(stored[0].attachment_id, item.attachment_id);
  assert.deepEqual(await service.refsFor(), []);
});

test("flush uploads every chunk, verifies digest and marks complete", async () => {
  const { repo, transport, service } = setup();
  const bytes = payloadOf(300_000);
  const item = await service.add("screen.png", "image/png", bytes, true);

  const completed = await service.flush();
  assert.equal(completed, 1);
  assert.equal(transport.initCalls.length, 1);
  assert.deepEqual(
    transport.chunkCalls.map((call) => call.index),
    [0, 1],
  );
  assert.equal(transport.chunkCalls[0].bytes.length, ATTACHMENT_CHUNK_BYTES);
  assert.equal(transport.chunkCalls[1].bytes.length, 300_000 - ATTACHMENT_CHUNK_BYTES);
  assert.equal(transport.completeCalls.length, 1);

  const stored = (await repo.load()).attachments[0];
  assert.equal(stored.status, "complete");
  assert.deepEqual(stored.uploaded_chunks, [0, 1]);
  assert.ok(stored.completed_at !== null);
  assert.deepEqual(await service.refsFor(), [item.attachment_id]);
  assert.deepEqual(await service.refsFor(["other"]), []);
});

test("flush resumes from server-reported chunk progress", async () => {
  const { repo, transport, service } = setup();
  const bytes = payloadOf(ATTACHMENT_CHUNK_BYTES + 100);
  const item = await service.add("screen.png", "image/png", bytes, true);
  transport.seed(
    item.attachment_id,
    0,
    bytes.subarray(0, ATTACHMENT_CHUNK_BYTES),
  );

  const completed = await service.flush();
  assert.equal(completed, 1);
  assert.deepEqual(transport.chunkCalls.map((call) => call.index), [1]);
  assert.equal((await repo.load()).attachments[0].status, "complete");
});

test("network failure marks failed and a later flush retries", async () => {
  const { repo, transport, service } = setup();
  const bytes = payloadOf(1000);
  await service.add("screen.png", "image/png", bytes, true);

  transport.failNextChunk = true;
  assert.equal(await service.flush(), 0);
  let stored = (await repo.load()).attachments[0];
  assert.equal(stored.status, "failed");
  assert.match(String(stored.last_error), /network down/);

  assert.equal(await service.flush(), 1);
  stored = (await repo.load()).attachments[0];
  assert.equal(stored.status, "complete");
  assert.deepEqual(stored.uploaded_chunks, [0]);
  assert.equal(stored.last_error, null);
});

test("chunk digest mismatch is rejected before completion", async () => {
  const { repo, transport, service } = setup();
  await service.add("screen.png", "image/png", payloadOf(1000), true);

  transport.badNextChunkSha = true;
  assert.equal(await service.flush(), 0);
  let stored = (await repo.load()).attachments[0];
  assert.equal(stored.status, "failed");
  assert.match(String(stored.last_error), /摘要不一致/);
  assert.deepEqual(stored.uploaded_chunks, []);
  assert.equal(transport.completeCalls.length, 0);

  assert.equal(await service.flush(), 1);
  stored = (await repo.load()).attachments[0];
  assert.equal(stored.status, "complete");
});

test("full digest mismatch clears progress and retry re-uploads", async () => {
  const { repo, transport, service } = setup();
  const bytes = payloadOf(2000);
  await service.add("screen.png", "image/png", bytes, true);

  transport.forceCompleteMismatch = true;
  assert.equal(await service.flush(), 0);
  let stored = (await repo.load()).attachments[0];
  assert.equal(stored.status, "failed");
  assert.match(String(stored.last_error), /未确认附件完整性/);
  assert.deepEqual(stored.uploaded_chunks, []);

  transport.forceCompleteMismatch = false;
  assert.equal(await service.flush(), 1);
  stored = (await repo.load()).attachments[0];
  assert.equal(stored.status, "complete");
  // 第二次 flush 重新上传（服务端已丢弃不可信分块）
  assert.equal(transport.chunkCalls.length, 2);
});

test("server 422 on complete clears progress for full retry", async () => {
  const { repo, transport, service } = setup();
  await service.add("screen.png", "image/png", payloadOf(1000), true);

  transport.throwOnComplete = 422;
  assert.equal(await service.flush(), 0);
  let stored = (await repo.load()).attachments[0];
  assert.equal(stored.status, "failed");
  assert.deepEqual(stored.uploaded_chunks, []);

  assert.equal(await service.flush(), 1);
  stored = (await repo.load()).attachments[0];
  assert.equal(stored.status, "complete");
});

test("after restart bytes are gone and flush reports failure", async () => {
  const repo = new OfflineRepository(new MemoryStore());
  const transport = new FakeAttachmentTransport();
  const first = new AttachmentService(repo, transport);
  await first.add("screen.png", "image/png", payloadOf(100), true);

  const second = new AttachmentService(repo, transport);
  assert.equal(await second.flush(), 0);
  const stored = (await repo.load()).attachments[0];
  assert.equal(stored.status, "failed");
  assert.match(String(stored.last_error), /重新选择文件/);
  assert.equal(transport.initCalls.length, 0);
});

test("re-adding identical content reuses the record and resumes chunks", async () => {
  const repo = new OfflineRepository(new MemoryStore());
  const transport = new FakeAttachmentTransport();
  const bytes = payloadOf(ATTACHMENT_CHUNK_BYTES + 100);

  const first = new AttachmentService(repo, transport);
  const original = await first.add("screen.png", "image/png", bytes, true);
  assert.equal(original.status, "pending");

  // 模拟进程重启后重新选择同一文件：新服务实例、无内存字节、新摘要计算
  const second = new AttachmentService(repo, transport);
  const reused = await second.add("screen.png", "image/png", bytes, true);
  assert.equal(reused.attachment_id, original.attachment_id);
  assert.deepEqual((await repo.load()).attachments.length, 1);

  transport.seed(
    reused.attachment_id,
    0,
    bytes.subarray(0, ATTACHMENT_CHUNK_BYTES),
  );
  assert.equal(await second.flush(), 1);
  assert.deepEqual(transport.chunkCalls.map((call) => call.index), [1]);
  const stored = (await repo.load()).attachments[0];
  assert.equal(stored.status, "complete");
  assert.deepEqual(stored.uploaded_chunks, [0, 1]);
});

test("re-adding completed content does not upload again", async () => {
  const repo = new OfflineRepository(new MemoryStore());
  const transport = new FakeAttachmentTransport();
  const bytes = payloadOf(1000);

  const first = new AttachmentService(repo, transport);
  const item = await first.add("screen.png", "image/png", bytes, true);
  assert.equal(await first.flush(), 1);
  assert.equal((await repo.load()).attachments[0].status, "complete");

  const second = new AttachmentService(repo, transport);
  const reused = await second.add("screen.png", "image/png", bytes, true);
  assert.equal(reused.attachment_id, item.attachment_id);
  assert.equal(reused.status, "complete");
  assert.equal(await second.flush(), 0);
  assert.equal(transport.initCalls.length, 1);
  assert.equal(transport.chunkCalls.length, 1);
  assert.equal(transport.completeCalls.length, 1);
});

test("different content creates a separate attachment", async () => {
  const { repo, service } = setup();
  const a = await service.add("a.png", "image/png", payloadOf(100), true);
  const b = await service.add("b.png", "image/png", payloadOf(200), true);
  assert.notEqual(a.attachment_id, b.attachment_id);
  assert.equal((await repo.load()).attachments.length, 2);
});

test("queue carries attachment refs and rejects invalid ones", async () => {
  const { repo } = setup();
  const queue = new EventQueue(repo);
  const refA = "att_0123456789abcdef";
  const refB = "att_fedcba9876543210";

  const event = await queue.enqueue({
    event_type: "client_task_queued",
    attachment_refs: [refA, refA, refB],
  });
  assert.deepEqual(event.attachment_refs, [refA, refB]);

  const plain = await queue.enqueue({ event_type: "client_note" });
  assert.deepEqual(plain.attachment_refs, []);

  await assert.rejects(
    queue.enqueue({ event_type: "client_note", attachment_refs: ["short"] }),
    /附件引用/,
  );
  await assert.rejects(
    queue.enqueue({
      event_type: "client_note",
      attachment_refs: Array.from(
        { length: 9 },
        (_, index) => `att_${String(index).padStart(16, "0")}`,
      ),
    }),
    /最多 8 项/,
  );
});
