/**
 * 安装回执上报（ALN-11-P）：台账记录 → 服务端回执 → 确认。
 *
 * 顺序固定：先落未确认台账条目，再尝试上传；上传失败条目保持未确认，
 * 由 flushInstallReceipts 重放（同一 apply_id 服务端幂等吸收）。
 * 任何路径都不允许在未上传成功时把条目标记为已确认。
 */

import type { ApplyResultRequest } from "../update/types.ts";
import type { InstallJournal } from "./journal.ts";

export interface InstallReceiptTransport {
  applyResult(request: ApplyResultRequest): Promise<void>;
}

export interface ReportInstallInput {
  kind: "installed" | "activated";
  release_id: string;
  version: string;
  artifact_digest: string;
  signing_key_id?: string;
  device_id: string;
}

export interface ReportInstallResult {
  ok: boolean;
  apply_id: string;
  error: string | null;
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** 记录并尝试上报一条安装/激活回执；断联时返回 ok=false 且条目未确认。 */
export async function reportInstall(
  journal: InstallJournal,
  transport: InstallReceiptTransport,
  input: ReportInstallInput,
): Promise<ReportInstallResult> {
  const entry = await journal.record({ ...input, kind: input.kind });
  const payload: ApplyResultRequest = {
    installation_id: entry.device_id,
    action: input.kind === "activated" ? "activated" : "verified",
    release_id: entry.release_id,
    version: entry.version,
    artifact_digest: entry.artifact_digest || undefined,
    signing_key_id: entry.signing_key_id || undefined,
    outcome: "ok",
    reason: "",
    apply_id: entry.apply_id,
  };
  try {
    await transport.applyResult(payload);
    await journal.confirm(entry.apply_id);
    return { ok: true, apply_id: entry.apply_id, error: null };
  } catch (error) {
    return { ok: false, apply_id: entry.apply_id, error: messageOf(error) };
  }
}

/** 重放全部未确认回执；返回仍剩余的 apply_id。 */
export async function flushInstallReceipts(
  journal: InstallJournal,
  transport: InstallReceiptTransport,
): Promise<string[]> {
  const entries = await journal.entries();
  const remaining: string[] = [];
  for (const entry of entries) {
    if (entry.server_confirmed) continue;
    const payload: ApplyResultRequest = {
      installation_id: entry.device_id,
      action: entry.kind === "retired"
        ? "retired"
        : entry.kind === "activated" ? "activated" : "verified",
      release_id: entry.release_id,
      version: entry.version,
      artifact_digest: entry.artifact_digest || undefined,
      signing_key_id: entry.signing_key_id || undefined,
      outcome: "ok",
      reason: entry.reason,
      apply_id: entry.apply_id,
    };
    try {
      await transport.applyResult(payload);
      await journal.confirm(entry.apply_id);
    } catch {
      remaining.push(entry.apply_id);
    }
  }
  return remaining;
}
