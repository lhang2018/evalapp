/**
 * 退役流程（ALN-11-P）：安装实例退役某个已发布版本。
 *
 * 规则：
 * - 退役必须写明原因（服务端同样拒绝无原因退役）；
 * - 同一版本+制品已有未确认退役条目时返回该条目，不重复上报；
 * - 上传失败返回 ok=false，条目保持未确认——断联不冒充已退役。
 */

import type { InstallJournal, InstallJournalEntry } from "./journal.ts";
import type { InstallReceiptTransport } from "./report.ts";

export interface RetireReleaseInput {
  release_id: string;
  version: string;
  artifact_digest: string;
  signing_key_id?: string;
  device_id: string;
  reason: string;
}

export interface RetireReleaseResult {
  ok: boolean;
  entry: InstallJournalEntry | null;
  error: string | null;
  duplicate: boolean;
}

export async function retireRelease(
  journal: InstallJournal,
  transport: InstallReceiptTransport,
  input: RetireReleaseInput,
): Promise<RetireReleaseResult> {
  if (!input.reason.trim()) {
    return { ok: false, entry: null, error: "退役必须写明原因", duplicate: false };
  }
  if (!input.release_id.trim() || !input.version.trim()) {
    return { ok: false, entry: null, error: "退役目标缺少版本标识", duplicate: false };
  }
  if (!input.device_id.trim()) {
    return { ok: false, entry: null, error: "退役必须绑定设备", duplicate: false };
  }
  const entries = await journal.entries();
  const pending = entries.find(
    (entry) => entry.kind === "retired"
      && !entry.server_confirmed
      && entry.release_id === input.release_id.trim()
      && entry.version === input.version.trim()
      && entry.artifact_digest === input.artifact_digest,
  );
  if (pending) {
    // 已有未确认退役条目：重放而非重复登记
    return { ok: false, entry: pending, error: "退役回执待确认", duplicate: true };
  }
  const entry = await journal.record({
    kind: "retired",
    release_id: input.release_id,
    version: input.version,
    artifact_digest: input.artifact_digest,
    signing_key_id: input.signing_key_id,
    device_id: input.device_id,
    reason: input.reason,
  });
  try {
    await transport.applyResult({
      installation_id: entry.device_id,
      action: "retired",
      release_id: entry.release_id,
      version: entry.version,
      artifact_digest: entry.artifact_digest || undefined,
      signing_key_id: entry.signing_key_id || undefined,
      outcome: "ok",
      reason: entry.reason,
      apply_id: entry.apply_id,
    });
    await journal.confirm(entry.apply_id);
    return { ok: true, entry, error: null, duplicate: false };
  } catch (error) {
    return {
      ok: false,
      entry,
      error: error instanceof Error ? error.message : String(error),
      duplicate: false,
    };
  }
}
