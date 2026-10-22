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
import { sha256Hex } from "../offline/sha256.ts";
import { ArtifactDownloader } from "./artifact.ts";
import type { ArtifactFileSystem, ArtifactSource } from "./artifact.ts";
import { TrustedKeyRing } from "./trust.ts";
import type {
  ApplyResultRequest, PendingReceipt, ReconcileResponse, UpdateSlot,
  UpdateState, UpdateTarget, UpdateTransport,
} from "./types.ts";

export const UPDATE_STATE_KEY = "update_state_v1";

export interface BundleApplyRequest {
  release_id: string;
  version: string;
  path: string;
  digest: string;
  slot: "a" | "b";
  bundle_ref?: string | null;
}

export interface BundleApplier {
  apply(request: BundleApplyRequest): Promise<void>;
}

export interface UpdateManagerOptions {
  appVersion: string;
  platform?: string;
  installationId?: string;
  toolContracts?: string[];
  now?: () => number;
  freeBytes?: () => number;
  minFreeBytes?: number;
  storeKey?: string;
  artifactSource?: ArtifactSource;
  artifactFiles?: ArtifactFileSystem;
  downloader?: ArtifactDownloader;
  trustedKeys?: TrustedKeyRing;
  bundleApplier?: BundleApplier;
  grantedPermissions?: string[] | (() => string[] | Promise<string[]>);
  activationAllowed?: () => boolean | Promise<boolean>;
  stagingDir?: string;
  maxArtifactBytes?: number;
}

function versionAtLeast(current: string, minimum: string): boolean {
  const parse = (value: string): number[] | null => {
    const match = /^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$/i.exec(value.trim());
    if (!match) return null;
    const parts = [match[1], match[2] ?? "0", match[3] ?? "0"].map(Number);
    return parts.every(Number.isSafeInteger) ? parts : null;
  };
  const left = parse(current);
  const right = parse(minimum);
  if (!left || !right) return false;
  for (let index = 0; index < 3; index += 1) {
    if (left[index] > right[index]) return true;
    if (left[index] < right[index]) return false;
  }
  return true;
}

function safeReleaseId(value: string): boolean {
  return /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value);
}

function realArtifactTarget(target: UpdateTarget | null): boolean {
  return Boolean(target && [target.artifact_url, target.artifact_sha256,
    target.artifact_signature, target.signing_key_id, target.signature_schema,
    target.artifact_size].some((value) => value !== undefined && value !== null));
}

function emptyState(installationId: string): UpdateState {
  return {
    version: 1,
    installation_id: installationId,
    active_slot: "a",
    slots: { a: null, b: null },
    last_known_good: null,
    pending: null,
    artifact: null,
    receipts: [],
    phase: "idle",
    target: null,
    checks: [],
    reasons: [],
    candidates: [],
    candidates_synced_at: null,
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
      candidates: Array.isArray(data.candidates) ? data.candidates : [],
      candidates_synced_at: data.candidates_synced_at ?? null,
      artifact: data.artifact ?? null,
      receipts: Array.isArray(data.receipts) ? data.receipts : [],
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
  private readonly downloader: ArtifactDownloader | null;
  private readonly trustedKeys: TrustedKeyRing | null;
  private readonly bundleApplier: BundleApplier | null;
  private readonly permissions: string[] | (() => string[] | Promise<string[]>) | null;
  private readonly activationAllowed: (() => boolean | Promise<boolean>) | null;
  private readonly stagingDir: string;
  private readonly maxArtifactBytes: number | undefined;
  private readonly artifactFiles: ArtifactFileSystem | null;
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
    this.artifactFiles = options.artifactFiles ?? null;
    this.downloader = options.downloader
      ?? (options.artifactSource && options.artifactFiles
        ? new ArtifactDownloader(options.artifactSource, options.artifactFiles)
        : null);
    this.trustedKeys = options.trustedKeys ?? null;
    this.bundleApplier = options.bundleApplier ?? null;
    this.permissions = options.grantedPermissions ?? null;
    this.activationAllowed = options.activationAllowed ?? null;
    this.stagingDir = options.stagingDir ?? "updates";
    this.maxArtifactBytes = options.maxArtifactBytes;
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
        } else if (!safeReleaseId(target.release_id) || !target.artifact_digest) {
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
        } else if (!versionAtLeast(this.appVersion, target.min_app_version)) {
          decision = "blocked";
          reasons.push("当前 App 版本低于目标最低版本");
        } else {
          const missing = (target.required_tool_contracts ?? [])
            .filter((name) => !this.toolContracts.includes(name));
          if (missing.length) {
            decision = "blocked";
            reasons.push("缺少工具契约: " + missing.join(", "));
          }
        }
      }

      state.target = decision === "update_available" ? target : null;
      if (state.target && state.artifact
          && state.artifact.release_id !== state.target.release_id) {
        state.artifact = null;
        state.pending = null;
      }
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

  /**
   * 拉取服务端候选阶段摘要（双端协同 §2）：只作透明展示，
   * 不改变本地激活路径——实际更新仍必须经过 reconcile 与签名校验。
   */
  refreshCandidates(): Promise<UpdateState> {
    return this.queue(async () => {
      const state = await this.load();
      try {
        const response = await this.transport.candidateStates(
          state.installation_id,
        );
        state.candidates = Array.isArray(response.candidates)
          ? response.candidates
          : [];
        state.candidates_synced_at = this.now();
      } catch (error) {
        state.last_error = `候选状态同步失败: ${messageOf(error)}`;
        await this.save(state);
        throw error;
      }
      await this.save(state);
      return state;
    });
  }

  /**
   * 真实制品路径：断点下载到暂存文件 → 受信公钥验证真实字节 →
   * 记录版本绑定的制品引用。任一步失败都不进入可激活状态。
   */
  prepare(): Promise<UpdateState> {
    return this.queue(async () => {
      const state = await this.load();
      if (state.phase !== "available" || !state.target) {
        throw new Error("没有可下载的候选版本");
      }
      const target = state.target;
      const fail = async (message: string): Promise<never> => {
        state.phase = "failed";
        state.last_error = message;
        state.artifact = null;
        state.pending = null;
        await this.save(state);
        throw new Error(message);
      };

      if (!safeReleaseId(target.release_id)) return fail("候选制品 ID 非法");

      if (!versionAtLeast(this.appVersion, target.min_app_version)) {
        return fail("当前 App 版本低于目标最低版本");
      }
      const missingContracts = (target.required_tool_contracts ?? [])
        .filter((name) => !this.toolContracts.includes(name));
      if (missingContracts.length) {
        return fail("缺少工具契约: " + missingContracts.join(", "));
      }
      const required = target.required_permissions ?? [];
      if (required.length) {
        const granted = await this.grantedPermissions();
        if (granted === null) {
          return fail("无法确认所需权限");
        }
        const missing = required.filter((name) => !granted.includes(name));
        if (missing.length) {
          return fail("缺少权限: " + missing.join(", "));
        }
      }
      if (!target.artifact_url || !target.artifact_sha256
          || !target.artifact_signature || !target.signing_key_id
          || !target.signature_schema || !Number.isSafeInteger(target.artifact_size)
          || (target.artifact_size ?? 0) <= 0) {
        return fail("候选版本缺少制品地址、摘要、签名或签名密钥");
      }
      if (!this.downloader || !this.trustedKeys) {
        return fail("客户端未配置下载器或受信公钥");
      }

      const expectedSize = target.artifact_size ?? 0;
      const free = await this.freeBytes();
      if (free < this.minFreeBytes + expectedSize) {
        return fail("存储空间不足，拒绝准备激活");
      }
      const stagingPath = `${this.stagingDir}/${target.release_id}.part`;
      const finalPath = `${this.stagingDir}/${target.release_id}.bundle`;
      let result;
      try {
        result = await this.downloader.download({
          url: target.artifact_url,
          sha256: target.artifact_sha256,
          sizeBytes: target.artifact_size ?? undefined,
          stagingPath,
          finalPath,
          maxBytes: this.maxArtifactBytes,
        });
      } catch (error) {
        await this.removeArtifact(finalPath);
        return fail(`制品下载失败: ${messageOf(error)}`);
      }

      const envelope = {
        schema: target.signature_schema ?? "evoagent.release.signing/1",
        release_id: target.release_id,
        version: target.version,
        artifact_sha256: target.artifact_sha256,
        artifact_size: target.artifact_size ?? result.size,
        signing_key_id: target.signing_key_id,
        signature: target.artifact_signature,
      };
      const verified = this.trustedKeys.verifyDigest(
        envelope, result.sha256, result.size);
      // 有独立 artifact_sha256 时以真实字节 + envelope 为准；
      // artifact_digest 是服务端元数据签名摘要，不能当作字节摘要比对。
      const declaredDigest = target.artifact_sha256
        ? ""
        : (target.artifact_digest ?? "")
          .toLowerCase().replace(/^sha256:/, "");
      if (!verified.ok
          || (declaredDigest && declaredDigest !== result.sha256)) {
        await this.removeArtifact(finalPath);
        const detail = verified.reasons.length
          ? verified.reasons.join("; ")
          : "制品摘要与对账声明不符";
        return fail(`制品验签失败: ${detail}`);
      }

      state.artifact = {
        release_id: target.release_id,
        version: target.version,
        path: result.path,
        size: result.size,
        digest: result.sha256,
        signing_key_id: target.signing_key_id,
        verified_at: this.now(),
      };
      state.pending = {
        release_id: target.release_id,
        version: target.version,
        digest: result.sha256,
        activated_at: 0,
      };
      state.phase = "verified";
      state.last_error = null;
      await this.save(state);
      await this.report(state, "verified", target.release_id, "ok", "", undefined,
        { version: target.version, digest: result.sha256,
          keyId: target.signing_key_id });
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
      if (realArtifactTarget(target) || this.downloader || this.trustedKeys
          || this.bundleApplier) {
        throw new Error("真实制品必须通过 prepare() 下载并验签");
      }
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
      const requiresArtifact = realArtifactTarget(state.target)
        || Boolean(this.downloader || this.trustedKeys || this.bundleApplier);
      if (this.activationAllowed
          && !(await this.activationAllowed())) {
        state.last_error = "当前时机不允许激活";
        await this.save(state);
        return state;
      }
      const inactive = state.active_slot === "a" ? "b" : "a";
      const artifact = state.artifact
        && state.artifact.release_id === candidate.release_id
        ? state.artifact : null;
      if (requiresArtifact || artifact) {
        try {
          if (!artifact || !this.artifactFiles || !this.trustedKeys || !state.target) {
            throw new Error("缺少已校验的真实制品或验签器");
          }
          if (!this.bundleApplier) throw new Error("缺少能力包装载器，不能激活");
          const target = state.target;
          if (artifact.version !== candidate.version || artifact.digest !== candidate.digest
              || target.release_id !== candidate.release_id
              || target.version !== candidate.version) {
            throw new Error("制品与待激活版本不匹配");
          }
          if (!versionAtLeast(this.appVersion, target.min_app_version)
              || (target.required_tool_contracts ?? []).some(
                (name) => !this.toolContracts.includes(name))) {
            throw new Error("当前 App 版本或工具契约不兼容");
          }
          const required = target.required_permissions ?? [];
          const granted = required.length ? await this.grantedPermissions() : [];
          if (required.some((name) => !granted?.includes(name))) {
            throw new Error("缺少所需权限，不能激活");
          }
          const bytes = await this.artifactFiles.read(artifact.path);
          const checked = this.trustedKeys.verifyArtifact({
            schema: target.signature_schema ?? "",
            release_id: candidate.release_id, version: candidate.version,
            artifact_sha256: artifact.digest, artifact_size: artifact.size,
            signing_key_id: artifact.signing_key_id,
            signature: target.artifact_signature ?? "",
          }, bytes);
          if (!checked.ok) throw new Error(`制品验签失败: ${checked.reasons.join("; ")}`);
        } catch (error) {
          state.phase = "failed";
          state.last_error = messageOf(error);
          await this.save(state);
          throw error;
        }
      }
      await this.report(state, "activation_pending", candidate.release_id,
        "ok", "", undefined, {
          version: candidate.version, digest: candidate.digest,
          keyId: artifact?.signing_key_id,
        });
      if (artifact) {
        if (!this.bundleApplier) {
          state.phase = "failed";
          state.last_error = "缺少能力包装载器，不能激活";
          await this.save(state);
          throw new Error(state.last_error);
        }
        try {
          await this.bundleApplier.apply({
            release_id: candidate.release_id,
            version: candidate.version,
            path: artifact.path,
            digest: artifact.digest,
            slot: inactive,
            bundle_ref: state.target?.bundle_ref ?? null,
          });
        } catch (error) {
          state.phase = "failed";
          state.last_error = `能力包装载失败: ${messageOf(error)}`;
          await this.report(state, "activation_failed", candidate.release_id,
            "failed", state.last_error, undefined, {
              version: candidate.version, digest: candidate.digest,
              keyId: artifact.signing_key_id,
            });
          await this.save(state);
          throw new Error(state.last_error);
        }
      }
      const previous = state.slots[state.active_slot];
      state.slots[inactive] = {
        ...candidate,
        activated_at: this.now(),
        signing_key_id: artifact?.signing_key_id,
        artifact_path: artifact?.path,
      };
      state.active_slot = inactive;
      // Real bundles become rollback candidates only after healthCheck(true).
      if (!artifact) state.last_known_good = previous;
      state.pending = null;
      state.artifact = null;
      state.phase = "active";
      await this.save(state);
      await this.report(state, "activated", candidate.release_id, "ok", "",
        undefined, {
          version: candidate.version, digest: candidate.digest,
          keyId: artifact?.signing_key_id,
        });
      return state;
    });
  }

  /** 健康检查：成功则把当前版本记为已知良好；失败自动回滚。 */
  healthCheck(ok: boolean): Promise<UpdateState> {
    return this.queue(async () => {
      const state = await this.load();
      const active = state.slots[state.active_slot];
      if (state.phase !== "active" || !active) {
        throw new Error("当前没有已激活的版本");
      }
      await this.report(state, "health_check", active.release_id,
        ok ? "ok" : "unhealthy", ok ? "" : "健康检查失败", undefined, {
          version: active.version, digest: active.digest,
          keyId: active.signing_key_id,
        });
      if (ok) {
        state.last_known_good = active;
        await this.save(state);
        return state;
      }
      return this.rollbackState(state, "健康检查失败");
    });
  }

  rollback(reason = "manual rollback"): Promise<UpdateState> {
    return this.queue(async () => this.rollbackState(await this.load(), reason));
  }

  private async rollbackState(state: UpdateState, reason: string): Promise<UpdateState> {
    const target = state.last_known_good;
    if (!target) throw new Error("没有可回滚的健康版本");
    const inactive = state.active_slot === "a" ? "b" : "a";
    if (target.signing_key_id) {
      if (!target.artifact_path || !this.artifactFiles || !this.bundleApplier) {
        throw new Error("缺少真实回滚制品或装载器");
      }
      const bytes = await this.artifactFiles.read(target.artifact_path);
      if (sha256Hex(bytes) !== target.digest) throw new Error("回滚制品摘要不符");
      await this.bundleApplier.apply({
        release_id: target.release_id, version: target.version,
        path: target.artifact_path, digest: target.digest, slot: inactive,
      });
    }
    if (state.slots[inactive]?.release_id !== target.release_id) {
      state.slots[inactive] = { ...target };
    }
    state.active_slot = inactive;
    state.pending = null;
    state.artifact = null;
    state.phase = "rolled_back";
    state.reasons = [...state.reasons, `回滚: ${reason}`];
    await this.save(state);
    await this.report(state, "rolled_back", target.release_id, "ok",
      reason, target.release_id, {
        version: target.version, digest: target.digest,
        keyId: target.signing_key_id,
      });
    return state;
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
    meta: { version?: string; digest?: string; keyId?: string } = {},
  ): Promise<void> {
    const receipt: PendingReceipt = {
      apply_id: createId("apply"),
      action,
      release_id: releaseId,
      version: meta.version,
      artifact_digest: meta.digest,
      signing_key_id: meta.keyId,
      outcome,
      reason,
      to_release_id: toReleaseId,
      attempts: 0,
      last_attempt_at: 0,
    };
    state.receipts = [...state.receipts, receipt];
    await this.save(state);
    const error = await this.deliver(state, receipt);
    if (!error) {
      state.receipts = state.receipts.filter(
        (item) => item.apply_id !== receipt.apply_id);
    } else {
      // 回执失败不回滚本地已完成的原子切换，留待补传
      state.last_error = `回执上传失败: ${error}`;
    }
    await this.save(state);
  }

  /** 重放未确认回执；同一 apply_id 重复上报由服务端幂等吸收。 */
  flushReceipts(): Promise<UpdateState> {
    return this.queue(async () => {
      const state = await this.load();
      const remaining: PendingReceipt[] = [];
      for (const receipt of state.receipts) {
        const error = await this.deliver(state, receipt);
        if (error) {
          remaining.push(receipt);
          state.last_error = `回执上传失败: ${error}`;
        }
      }
      state.receipts = remaining;
      await this.save(state);
      return state;
    });
  }

  private async deliver(state: UpdateState,
                        receipt: PendingReceipt): Promise<string | null> {
    receipt.attempts += 1;
    receipt.last_attempt_at = this.now();
    const payload: ApplyResultRequest = {
      installation_id: state.installation_id,
      action: receipt.action,
      release_id: receipt.release_id,
      outcome: receipt.outcome,
      reason: receipt.reason,
      apply_id: receipt.apply_id,
    };
    if (receipt.to_release_id) payload.to_release_id = receipt.to_release_id;
    if (receipt.version) payload.version = receipt.version;
    if (receipt.artifact_digest) {
      payload.artifact_digest = receipt.artifact_digest;
    }
    if (receipt.signing_key_id) {
      payload.signing_key_id = receipt.signing_key_id;
    }
    try {
      await this.transport.applyResult(payload);
      return null;
    } catch (error) {
      return messageOf(error);
    }
  }

  private async grantedPermissions(): Promise<string[] | null> {
    if (this.permissions === null) return null;
    if (typeof this.permissions === "function") {
      return this.permissions();
    }
    return this.permissions;
  }

  private async removeArtifact(path: string): Promise<void> {
    if (!this.artifactFiles) return;
    try {
      if (await this.artifactFiles.stat(path)) {
        await this.artifactFiles.remove(path);
      }
    } catch {
      // 清理失败不掩盖原始验签/下载错误
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
