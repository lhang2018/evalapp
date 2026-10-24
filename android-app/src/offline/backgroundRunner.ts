/**
 * 后台同步的 RN 组装层：只读取持久化配置，不依赖 React 组件树。
 * Node 测试不导入本文件（它依赖 AsyncStorage）。
 */

import { api } from "../api.ts";
import { createTaskNotifier } from "../device/nativeNotifications.ts";
import { asyncStorageStore } from "../storage/asyncStorage.ts";
import { ClientTransport } from "./apiTransport.ts";
import { runBackgroundSync } from "./background.ts";
import type { BackgroundSyncResult } from "./background.ts";
import { BACKGROUND_SYNC_TASK } from "./background.ts";
import { EventQueue } from "./queue.ts";
import { OfflineRepository } from "./storage.ts";
import { SyncEngine } from "./sync.ts";
import { LocalTaskService } from "./tasks.ts";

const BASE_URL_KEY = "evoagent_base_url";
const DEFAULT_BASE_URL = "http://10.0.2.2:8300";
const APP_VERSION = "0.1.0";

export async function runStoredBackgroundSync(): Promise<BackgroundSyncResult> {
  const startedAt = Date.now();
  try {
    const saved = await asyncStorageStore.getItem(BASE_URL_KEY);
    const baseUrl = (saved ?? "").trim() || DEFAULT_BASE_URL;
    const client = api(baseUrl);
    const repo = new OfflineRepository(asyncStorageStore);
    const queue = new EventQueue(repo);
    const transport = new ClientTransport(client);
    const tasks = new LocalTaskService(
      repo,
      transport,
      undefined,
      `client-manifest-${APP_VERSION}`,
    );
    const sync = new SyncEngine(queue, transport, tasks);
    const before = await tasks.list();
    const result = await runBackgroundSync({
      health: () => client.health(),
      flush: () => sync.flush(false),
      refresh: () => tasks.refresh(),
    });
    // 终态跃迁才提醒；通知失败不影响同步结果
    const notified = await createTaskNotifier().notifyTransitions(
      before,
      await tasks.list(),
    );
    console.log(
      `[background-sync] online=${result.online} flushed=${result.ran} ` +
        `refreshed=${result.refreshed} notified=${notified} ` +
        `error=${result.error ?? "none"}`,
    );
    return result;
  } catch (error) {
    console.log(`[background-sync] failed: ${error}`);
    return {
      task: BACKGROUND_SYNC_TASK,
      ran: false,
      online: false,
      refreshed: false,
      started_at: startedAt,
      finished_at: Date.now(),
      error: error instanceof Error ? error.message : String(error),
    };
  }
}
