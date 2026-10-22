/**
 * 能力包（workflow bundle）装载：校验 JSON 结构后写入目标槽，
 * 并记录当前生效槽。只有已经过字节验签的制品才会进入这里。
 */

import type { ArtifactFileSystem } from "./artifact.ts";
import { sha256Hex } from "../offline/sha256.ts";
import { utf8Decode } from "./base64.ts";
import type { KeyValueStore } from "../offline/storage.ts";
import type { BundleApplyRequest, BundleApplier } from "./manager.ts";

export const ACTIVE_BUNDLE_KEY = "update_bundle_active_v1";
export const SLOT_BUNDLE_PREFIX = "update_bundle_v1:";

export interface InstalledBundle {
  release_id: string;
  version: string;
  slot: "a" | "b";
  digest: string;
  bundle_ref?: string | null;
  installed_at: number;
  bundle: Record<string, unknown>;
}

export function slotBundleKey(slot: "a" | "b"): string {
  return `${SLOT_BUNDLE_PREFIX}${slot}`;
}

function parseBundle(bytes: Uint8Array): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(utf8Decode(bytes));
  } catch {
    throw new Error("能力包不是合法 JSON");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("能力包必须是对象");
  }
  const bundle = parsed as Record<string, unknown>;
  if (typeof bundle.workflow !== "string" || !bundle.workflow.trim()) {
    throw new Error("能力包缺少 workflow 标识");
  }
  return bundle;
}

export class WorkflowBundleApplier implements BundleApplier {
  private readonly fs: ArtifactFileSystem;
  private readonly kv: KeyValueStore;
  private readonly now: () => number;

  constructor(fs: ArtifactFileSystem, kv: KeyValueStore,
              now: () => number = () => Date.now()) {
    this.fs = fs;
    this.kv = kv;
    this.now = now;
  }

  async apply(request: BundleApplyRequest): Promise<void> {
    const bytes = await this.fs.read(request.path);
    if (sha256Hex(bytes) !== request.digest) {
      throw new Error("装载时制品摘要不符");
    }
    const bundle = parseBundle(bytes);
    const record: InstalledBundle = {
      release_id: request.release_id,
      version: request.version,
      slot: request.slot,
      digest: request.digest,
      bundle_ref: request.bundle_ref ?? null,
      installed_at: this.now(),
      bundle,
    };
    // 先写目标槽，再指向生效槽：中断时旧槽保持可用。
    await this.kv.setItem(slotBundleKey(request.slot),
                          JSON.stringify(record));
    await this.kv.setItem(ACTIVE_BUNDLE_KEY, JSON.stringify({
      slot: record.slot,
      release_id: record.release_id,
      version: record.version,
      digest: record.digest,
      installed_at: record.installed_at,
    }));
  }
}

export async function loadActiveBundle(
  kv: KeyValueStore,
): Promise<InstalledBundle | null> {
  const raw = await kv.getItem(ACTIVE_BUNDLE_KEY);
  if (!raw) return null;
  let marker: { slot?: unknown } | null = null;
  try {
    marker = JSON.parse(raw) as { slot?: unknown };
  } catch {
    return null;
  }
  if (!marker || (marker.slot !== "a" && marker.slot !== "b")) return null;
  const stored = await kv.getItem(slotBundleKey(marker.slot));
  if (!stored) return null;
  try {
    return JSON.parse(stored) as InstalledBundle;
  } catch {
    return null;
  }
}
