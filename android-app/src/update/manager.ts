/**
 * 客户端更新管理（双端协同 §5.2/§5.3 / §10.3）。
 *
 * 流程：对账 → 校验签名/兼容/资源 → 写入非活动槽位 → 原子切换 →
 * 健康检查 → 晋级或回滚。只处理可独立版本化的配置/能力包；
 * 原生 APK 必须走平台应用更新机制，这里只将其标记为需二进制更新。
 */

import type { KeyValueStore } from "../offline/storage.ts";
import { createId } from "../offline/ids.ts";
import { getInstallationId } from "./identity.ts";
import type { ApplyResultRequest, ReconcileResponse, UpdateSlot, UpdateState, UpdateTarget, UpdateTransport } from "./types.ts";

export const UPDATE_STATE_KEY = "update_state_v1";

export interface UpdateManagerOptions {
  appVersion: string;
  platform?: string;
  installationId?: string;
  toolContracts?: string[];
  now?: () => number;
  freeBytes?: () => number;
  minFreeBytes?: number;
  storeKey?: string;
}

function emptyState(installationId: string): UpdateState {
  return {
    version: 1,
    installation_id: installationId,
    active_slot: "a",
    slots: { a: null, b: null },
    last_known_good: null,
    pending: null,
    phase: "idle",
    target: null,
    checks: [],
    reasons: [],
    last_checked_at: null,
    last_error: null,
  };
}

function parseState(raw: string | null, fallbackId: string): UpdateState {
  if (!raw) return emptyState(fallbackId);
  try {
    const data = JSON.parse(raw) as Partial<UpdateState>;
    if (!data || data.version !== 1) return emptyState(fallbackId);
    const base = emptyState(
      typeof data.installation_id === "string" && data.installation_id
        ? data.installation_id
        : fallbackId,
    );
    return {
      ...base,
      ...data,
      slots: {
        a: data.slots?.a ?? null,
        b: data.slots?.b ?? null,
      },
      checks: Array.isArray(data.checks) ? data.checks : [],
      reasons: Array.isArray(data.reasons) ? data.reasons : [],
      last_error: data.last_error ?? null,
    };
  } catch {
    return emptyState(fallbackId);
  }
}

export class UpdateManager {
  private readonly transport: UpdateTransport;
  private readonly kv: KeyValueStore;
  private readonly appVersion: string;
  private readonly platform: string;
  private readonly installationId: string | null;
  private readonly toolContracts: string[];
  private readonly now: () => number;
  private readonly freeBytes: () => number;
  private readonly minFreeBytes: number;
  private readonly storeKey: string;
  private chain: Promise<unknown> = Promise.resolve();

  constructor(
    transport: UpdateTransport,
    kv: KeyValueStore,
    options: UpdateManagerOptions,
  ) {
    this.transport = transport;
    this.kv = kv;
    this.appVersion = options.appVersion;
    this.platform = options.platform ?? "android";
    this.installationId = options.installationId ?? null;
    this.toolContracts = options.toolContracts ?? [];
    this.now = options.now ?? (() => Date.now());
    this.freeBytes = options.freeBytes ?? (() => Number.MAX_SAFE_INTEGER);
    this.minFreeBytes = options.minFreeBytes ?? 1024 * 1024;
    this.storeKey = options.storeKey ?? UPDATE_STATE_KEY;
  }

  init(): Promise<UpdateState> {
    return this.queue(async () => {
      const id = this.installationId ?? (await getInstallationId(this.kv));
      const state = parseState(await this.kv.getItem(this.storeKey), id);
      await this.save(state);
      return state;
    });
  }

  status(): Promise<UpdateState> {
    return this.queue(async () => {
      const id = this.installationId ?? (await getInstallationId(this.kv));
      return parseState(await this.kv.getItem(this.storeKey), id);
    });
  }

  /** 向服务端对账；本地再做一次兼容与交付方式校验（不信任单一来源）。 */
  check(): Promise<UpdateState> {
    return this.queue(async () => {
      const state = await this.load();
      let response: ReconcileResponse;
      try {
        response = await this.transport.reconcile({
          installation_id: state.installation_id,
          platform: this.platform,
          app_version: this.appVersion,
          current_release_id: this.activeReleaseId(state),
          tool_contracts: this.toolContracts,
        });
      } catch (error) {
        state.phase = "failed";
        state.last_error = messageOf(error);
        await this.save(state);
        throw error;
      }

      const reasons = [...(response.reasons ?? [])];
      let decision = response.decision;
      const target = response.target;
      if (decision === "update_available") {
        if (!target) {
          decision = "blocked";
          reasons.push("服务端未返回目标版本");
        } else if (!target.release_id || !target.artifact_digest) {
          decision = "blocked";
          reasons.push("目标版本缺少 ID 或制品摘要");
        } else if (!target.signature_ok) {
          decision = "blocked";
          reasons.push("目标版本签名或制品完整性校验失败");
        } else if (target.delivery_mode !== "capability_slot") {
          decision = "blocked";
          reasons.push("需要客户端二进制更新，必须走平台应用更新机制");
        } else if (!this.hasRollbackPath(state, target)) {
          decision = "blocked";
          reasons.push("没有可用的回滚目标或恢复路径");
        }
      }

      state.target = decision === "update_available" ? target : null;
      state.checks = response.checks ?? [];
      state.reasons = reasons;
      state.last_checked_at = this.now();
      state.last_error = decision === "blocked" ? reasons.join("; ") : null;
      if (decision === "update_available") {
        state.phase = "available";
      } else if (decision === "blocked") {
        state.phase = "blocked";
      } else {
        state.phase = state.slots[state.active_slot] ? "active" : "idle";
      }
      await this.save(state);
      return state;
    });
  }

  /** 下载后校验：预留空间不足或签名/摘要缺失时拒绝进入可激活状态。 */
  verify(downloadBytes = 0): Promise<UpdateState> {
    return this.queue(async () => {
      const state = await this.load();
      if (state.phase !== "available" || !state.target) {
        throw new Error("没有可验证的候选版本");
      }
      const target = state.target;
      if (!target.artifact_digest || !target.signature_ok) {
        state.phase = "failed";
        state.last_error = "候选版本签名或摘要不可信";
        await this.save(state);
        throw new Error(state.last_error);
      }
      const free = await this.freeBytes();
      if (free < this.minFreeBytes + Math.max(0, downloadBytes)) {
        state.phase = "failed";
        state.last_error = "存储空间不足，拒绝准备激活";
        await this.save(state);
        throw new Error(state.last_error);
      }
      state.pending = {
        release_id: target.release_id,
        version: target.version,
        digest: target.artifact_digest,
        activated_at: 0,
      };
      state.phase = "verified";
      state.last_error = null;
      await this.save(state);
      await this.report(state, "verified", target.release_id);
      return state;
    });
  }

  /** 原子切换槽位引用；上一版本保留为回滚目标。 */
  activate(): Promise<UpdateState> {
    return this.queue(async () => {
      const state = await this.load();
      if (state.phase !== "verified" || !state.pending) {
        throw new Error("候选版本尚未通过校验，不能激活");
      }
      const candidate = state.pending;
      await this.report(state, "activation_pending", candidate.release_id);
      const previous = state.slots[state.active_slot];
      const inactive = state.active_slot === "a" ? "b" : "a";
      state.slots[inactive] = {
        ...candidate,
        activated_at: this.now(),
      };
      state.active_slot = inactive;
      state.last_known_good = previous;
      state.pending = null;
      state.phase = "active";
      await this.save(state);
      await this.report(state, "activated", candidate.release_id);
      return state;
    });
  }

  /** 健康检查：成功则把当前版本记为已知良好；失败自动回滚。 */
  async healthCheck(ok: boolean): Promise<UpdateState> {
    const state = await this.load();
    const active = state.slots[state.active_slot];
    if (state.phase !== "active" || !active) {
      throw new Error("当前没有已激活的版本");
    }
    await this.report(state, "health_check", active.release_id,
      ok ? "ok" : "unhealthy", ok ? "" : "健康检查失败");
    if (ok) {
      return this.queue(async () => {
        const current = await this.load();
        current.last_known_good = current.slots[current.active_slot];
        current.last_error = null;
        await this.save(current);
        return current;
      });
    }
    return this.rollback("健康检查失败");
  }

  rollback(reason = "manual rollback"): Promise<UpdateState> {
    return this.queue(async () => {
      const state = await this.load();
      const target = state.last_known_good;
      if (!target) throw new Error("没有可回滚的健康版本");
      const inactive = state.active_slot === "a" ? "b" : "a";
      if (state.slots[inactive]?.release_id !== target.release_id) {
        state.slots[inactive] = { ...target };
      }
      state.active_slot = inactive;
      state.pending = null;
      state.phase = "rolled_back";
      state.reasons = [...state.reasons, `回滚: ${reason}`];
      await this.save(state);
      await this.report(state, "rolled_back", target.release_id, "ok",
        reason, target.release_id);
      return state;
    });
  }

  // ---- 内部 ----
  private load(): Promise<UpdateState> {
    return (async () => {
      const id = this.installationId ?? (await getInstallationId(this.kv));
      return parseState(await this.kv.getItem(this.storeKey), id);
    })();
  }

  private save(state: UpdateState): Promise<void> {
    return this.kv.setItem(this.storeKey, JSON.stringify(state));
  }

  private activeReleaseId(state: UpdateState): string | null {
    return state.slots[state.active_slot]?.release_id ?? null;
  }

  private hasRollbackPath(state: UpdateState, target: UpdateTarget): boolean {
    // 已激活槽位本身就是已知可用版本；首次安装无需回滚。
    if (!state.slots[state.active_slot]) return true;
    return Boolean(target.rollback_target) ||
      Boolean(state.slots[state.active_slot]);
  }

  private async report(
    state: UpdateState,
    action: ApplyResultRequest["action"],
    releaseId: string,
    outcome = "ok",
    reason = "",
    toReleaseId?: string,
  ): Promise<void> {
    const payload: ApplyResultRequest = {
      installation_id: state.installation_id,
      action,
      release_id: releaseId,
      outcome,
      reason,
      apply_id: createId("apply"),
    };
    if (toReleaseId) payload.to_release_id = toReleaseId;
    try {
      await this.transport.applyResult(payload);
    } catch (error) {
      // 回执失败不回滚本地已完成的原子切换，留待下次对账补传
      const current = await this.load();
      current.last_error = `回执上传失败: ${messageOf(error)}`;
      await this.save(current);
    }
  }

  private queue<T>(fn: () => Promise<T>): Promise<T> {
    const next = this.chain.then(fn, fn);
    this.chain = next.catch(() => undefined);
    return next;
  }
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
