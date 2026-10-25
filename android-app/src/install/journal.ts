/**
 * 安装台账（ALN-11-P）：把安装/激活/退役回执绑定到真实设备与制品。
 *
 * 核心不变量——断联不冒充已安装：
 *   台账条目落地时 server_confirmed=false；只有 transport.applyResult
 *   真实成功后才置 true。status() 因此可以区分
 *   “已安装且服务端已确认”与“本地已激活但回执未确认”，UI 不得把
 *   后者显示为已安装。
 *
 * 台账与 update/manager.ts 的 update_state_v1 分开存储：本模块只追加
 * 记录，不改动已接受切片的状态结构。
 */

import type { KeyValueStore } from "../offline/storage.ts";
import { createId } from "../offline/ids.ts";

export type InstallKind = "installed" | "activated" | "retired";

export type InstallTrust =
  | "none"
  | "installed_confirmed"
  | "installed_unconfirmed"
  | "retired"
  | "retired_unconfirmed";

export interface InstallJournalEntry {
  entry_id: string;
  kind: InstallKind;
  release_id: string;
  version: string;
  artifact_digest: string;
  signing_key_id: string;
  device_id: string;
  reason: string;
  at: number;
  apply_id: string;
  server_confirmed: boolean;
  confirmed_at: number | null;
}

export interface InstallStatus {
  device_id: string;
  active_release_id: string | null;
  active_version: string | null;
  /** 当前活动条目的信任态；none 表示从未记录安装。 */
  trust: InstallTrust;
  /** 全部未确认回执的 apply_id（断联时非空）。 */
  pending_apply_ids: string[];
}

export interface InstallRecordInput {
  kind: InstallKind;
  release_id: string;
  version: string;
  artifact_digest: string;
  signing_key_id?: string;
  device_id: string;
  reason?: string;
}

export interface InstallJournalOptions {
  storeKey?: string;
  now?: () => number;
}

export const INSTALL_JOURNAL_KEY = "install_journal_v1";
const JOURNAL_VERSION = 1;
const APPLY_ID_RE = /^[A-Za-z0-9_-]{8,64}$/;

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function parseEntries(raw: string | null): InstallJournalEntry[] {
  if (!raw) return [];
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    // 台账损坏不抛异常：按空台账恢复，已确认状态由服务端回执补传重建
    return [];
  }
  if (!data || typeof data !== "object") return [];
  const doc = data as { version?: unknown; entries?: unknown };
  if (doc.version !== JOURNAL_VERSION || !Array.isArray(doc.entries)) {
    return [];
  }
  const entries: InstallJournalEntry[] = [];
  for (const item of doc.entries) {
    if (!item || typeof item !== "object") continue;
    const entry = item as Partial<InstallJournalEntry>;
    const kind = entry.kind;
    if (kind !== "installed" && kind !== "activated" && kind !== "retired") {
      continue;
    }
    if (!text(entry.release_id) || !text(entry.version)
        || !text(entry.device_id) || !text(entry.apply_id)) {
      continue;
    }
    if (!APPLY_ID_RE.test(text(entry.apply_id))) continue;
    if (typeof entry.at !== "number" || !Number.isFinite(entry.at)) continue;
    entries.push({
      entry_id: text(entry.entry_id) || createId("inj"),
      kind,
      release_id: text(entry.release_id),
      version: text(entry.version),
      artifact_digest: text(entry.artifact_digest),
      signing_key_id: text(entry.signing_key_id),
      device_id: text(entry.device_id),
      reason: text(entry.reason),
      at: entry.at,
      apply_id: text(entry.apply_id),
      server_confirmed: entry.server_confirmed === true,
      confirmed_at: typeof entry.confirmed_at === "number"
        ? entry.confirmed_at
        : null,
    });
  }
  return entries;
}

export class InstallJournal {
  private readonly kv: KeyValueStore;
  private readonly storeKey: string;
  private readonly now: () => number;

  constructor(kv: KeyValueStore, options: InstallJournalOptions = {}) {
    this.kv = kv;
    this.storeKey = options.storeKey ?? INSTALL_JOURNAL_KEY;
    this.now = options.now ?? (() => Date.now());
  }

  async entries(): Promise<InstallJournalEntry[]> {
    return parseEntries(await this.kv.getItem(this.storeKey));
  }

  /** 追加一条未确认记录；apply_id 由本模块生成，服务端据此幂等去重。 */
  async record(input: InstallRecordInput): Promise<InstallJournalEntry> {
    if (!input.release_id.trim() || !input.version.trim()) {
      throw new Error("安装台账条目缺少版本标识");
    }
    if (!input.device_id.trim()) {
      throw new Error("安装台账条目必须绑定设备");
    }
    const entry: InstallJournalEntry = {
      entry_id: createId("inj"),
      kind: input.kind,
      release_id: input.release_id.trim(),
      version: input.version.trim(),
      artifact_digest: input.artifact_digest ?? "",
      signing_key_id: input.signing_key_id ?? "",
      device_id: input.device_id.trim(),
      reason: input.reason ?? "",
      at: this.now(),
      apply_id: createId("apply"),
      server_confirmed: false,
      confirmed_at: null,
    };
    const entries = await this.entries();
    entries.push(entry);
    await this.persist(entries);
    return entry;
  }

  /** 回执上传成功后调用；重复确认幂等。 */
  async confirm(applyId: string): Promise<InstallJournalEntry | null> {
    const entries = await this.entries();
    let confirmed: InstallJournalEntry | null = null;
    for (const entry of entries) {
      if (entry.apply_id === applyId && !entry.server_confirmed) {
        entry.server_confirmed = true;
        entry.confirmed_at = this.now();
        confirmed = entry;
      }
    }
    if (confirmed) await this.persist(entries);
    return confirmed;
  }

  /**
   * 派生安装信任态。规则：
   * - 最新条目为 retired：retired / retired_unconfirmed（按确认状态）；
   * - 最新条目为 installed/activated：按确认状态给出
   *   installed_confirmed / installed_unconfirmed——未确认时绝不显示已安装；
   * - 无条目：none。
   */
  async status(): Promise<InstallStatus> {
    const entries = await this.entries();
    const latest = entries.length ? entries[entries.length - 1] : null;
    const pending = entries
      .filter((entry) => !entry.server_confirmed)
      .map((entry) => entry.apply_id);
    if (!latest) {
      return {
        device_id: "",
        active_release_id: null,
        active_version: null,
        trust: "none",
        pending_apply_ids: pending,
      };
    }
    const retired = latest.kind === "retired";
    const unconfirmed = !latest.server_confirmed;
    let trust: InstallTrust;
    if (retired) {
      trust = unconfirmed ? "retired_unconfirmed" : "retired";
    } else {
      trust = unconfirmed ? "installed_unconfirmed" : "installed_confirmed";
    }
    return {
      device_id: latest.device_id,
      active_release_id: retired ? null : latest.release_id,
      active_version: retired ? null : latest.version,
      trust,
      pending_apply_ids: pending,
    };
  }

  private async persist(entries: InstallJournalEntry[]): Promise<void> {
    await this.kv.setItem(this.storeKey, JSON.stringify({
      version: JOURNAL_VERSION,
      entries,
    }));
  }
}
