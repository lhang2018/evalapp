/** 后台同步原生模块的安全封装：缺失模块时降级，不影响前台功能。 */

import {
  DEFAULT_BACKGROUND_INTERVAL_MINUTES,
  normalizeBackgroundInterval,
} from "./background.ts";

export interface BackgroundSyncStatus {
  available: boolean;
  enabled: boolean;
  interval_minutes: number;
  last_attempt_at: number | null;
}

export interface BackgroundSyncModuleLike {
  backgroundSyncStatus(): Promise<unknown>;
  scheduleBackgroundSync(
    intervalMinutes: number,
    runNow: boolean,
  ): Promise<unknown>;
  cancelBackgroundSync(): Promise<unknown>;
  runBackgroundSyncNow(): Promise<unknown>;
}

function normalizeStatus(raw: unknown, available: boolean): BackgroundSyncStatus {
  const value =
    raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const last = value.last_attempt_at;
  return {
    available,
    enabled: value.enabled === true,
    interval_minutes: normalizeBackgroundInterval(
      typeof value.interval_minutes === "number"
        ? value.interval_minutes
        : DEFAULT_BACKGROUND_INTERVAL_MINUTES,
    ),
    last_attempt_at:
      typeof last === "number" && Number.isFinite(last) && last > 0
        ? last
        : null,
  };
}

const UNAVAILABLE: BackgroundSyncStatus = {
  available: false,
  enabled: false,
  interval_minutes: DEFAULT_BACKGROUND_INTERVAL_MINUTES,
  last_attempt_at: null,
};

export class NativeBackgroundSync {
  private readonly module: BackgroundSyncModuleLike | null;

  constructor(module: BackgroundSyncModuleLike | null) {
    this.module = module;
  }

  async status(): Promise<BackgroundSyncStatus> {
    if (!this.module) return { ...UNAVAILABLE };
    try {
      return normalizeStatus(await this.module.backgroundSyncStatus(), true);
    } catch {
      return { ...UNAVAILABLE };
    }
  }

  async enable(
    intervalMinutes: number,
    runNow: boolean = false,
  ): Promise<BackgroundSyncStatus> {
    if (!this.module) {
      throw new Error("当前构建未提供后台同步原生模块");
    }
    return normalizeStatus(
      await this.module.scheduleBackgroundSync(
        normalizeBackgroundInterval(intervalMinutes),
        runNow,
      ),
      true,
    );
  }

  async disable(): Promise<BackgroundSyncStatus> {
    if (!this.module) {
      throw new Error("当前构建未提供后台同步原生模块");
    }
    return normalizeStatus(await this.module.cancelBackgroundSync(), true);
  }

  async runNow(): Promise<BackgroundSyncStatus> {
    if (!this.module) {
      throw new Error("当前构建未提供后台同步原生模块");
    }
    return normalizeStatus(await this.module.runBackgroundSyncNow(), true);
  }
}
