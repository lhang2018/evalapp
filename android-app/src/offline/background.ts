/**
 * 后台同步（补充 PRD 72-76）：系统调度只复用既有队列与幂等提交。
 *
 * 本模块保持纯 TS，便于 Node 测试；真正的 WorkManager / Headless JS 接线在
 * backgroundRunner.ts 与 Kotlin 层。后台任务只做「健康检查 → 同步事件与任务
 * → 刷新状态」，不处理 NEEDS_RECONCILE（必须由用户显式确认），也不重放设备动作。
 */

export const BACKGROUND_SYNC_TASK = "EvoAgentBackgroundSync";
export const BACKGROUND_SYNC_WORK = "evoagent-background-sync";
export const MIN_BACKGROUND_INTERVAL_MINUTES = 15;
export const MAX_BACKGROUND_INTERVAL_MINUTES = 24 * 60;
export const DEFAULT_BACKGROUND_INTERVAL_MINUTES = 30;

export interface BackgroundSyncResult {
  task: typeof BACKGROUND_SYNC_TASK;
  ran: boolean;
  online: boolean;
  refreshed: boolean;
  started_at: number;
  finished_at: number;
  error: string | null;
}

export interface BackgroundSyncDeps {
  health: () => Promise<unknown>;
  flush: () => Promise<unknown>;
  refresh: () => Promise<unknown>;
  now?: () => number;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** WorkManager 周期任务最短 15 分钟；越界值夹紧，非法值回退默认。 */
export function normalizeBackgroundInterval(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_BACKGROUND_INTERVAL_MINUTES;
  return Math.min(
    MAX_BACKGROUND_INTERVAL_MINUTES,
    Math.max(MIN_BACKGROUND_INTERVAL_MINUTES, Math.round(value)),
  );
}

/**
 * 单次后台同步。网络不可用时安静退出（返回 ran=false），不抛异常，
 * 避免 Headless JS 任务因未捕获错误反复唤醒。
 */
export async function runBackgroundSync(
  deps: BackgroundSyncDeps,
): Promise<BackgroundSyncResult> {
  const now = deps.now ?? (() => Date.now());
  const startedAt = now();
  const result: BackgroundSyncResult = {
    task: BACKGROUND_SYNC_TASK,
    ran: false,
    online: false,
    refreshed: false,
    started_at: startedAt,
    finished_at: startedAt,
    error: null,
  };

  try {
    await deps.health();
  } catch (error) {
    result.error = errorMessage(error);
    result.finished_at = now();
    return result;
  }

  result.ran = true;
  result.online = true;
  try {
    await deps.flush();
  } catch (error) {
    // 同步失败由队列自己记录退避/死信；后台只回报本次错误
    result.error = errorMessage(error);
    result.finished_at = now();
    return result;
  }

  try {
    await deps.refresh();
    result.refreshed = true;
  } catch (error) {
    result.error = errorMessage(error);
  }
  result.finished_at = now();
  return result;
}
