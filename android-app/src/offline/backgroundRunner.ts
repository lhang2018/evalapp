/**
 * 后台同步的 RN 组装层：只读取持久化配置，不依赖 React 组件树。
 * Node 测试不导入本文件（它依赖 AsyncStorage）。
 */

import { api } from "../api.ts";
import { createTaskNotifier } from "../device/nativeNotifications.ts";
import { TaskNotifier } from "../device/notifications.ts";
import { asyncStorageStore } from "../storage/asyncStorage.ts";
import { ClientTransport } from "./apiTransport.ts";
import { runBackgroundSync } from "./background.ts";
import type { BackgroundSyncResult } from "./background.ts";
import type { LocalTask } from "./types.ts";
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
    // 通知快照是附加能力；读取失败不阻断实际同步。
    let before: LocalTask[] | null = null;
    try {
      before = await tasks.list();
    } catch {
      // 缺少可靠前态时不建立基线、不猜测跃迁；已建立基线仍可支持补发。
    }
    // 同步前只做"历史终态基线化"（不触碰原生通知模块）：避免首次安装/升级
    // 把既有已完成任务当成新完成而补发；失败不阻断同步，下次 run 重试。
    try {
      await TaskNotifier.primeBaseline(asyncStorageStore, before);
    } catch {
      // 基线初始化失败：保持未就绪，本次不补发。
    }
    const result = await runBackgroundSync({
      health: () => client.health(),
      flush: async () => {
        const flushed = await sync.flush(false);
        if (!flushed.ok) throw new Error(flushed.error ?? "同步失败");
        return flushed;
      },
      refresh: () => tasks.refresh(),
    });
    // 只有完成刷新才比较终态；原生桥只在确实需要投递时查找（失败不影响同步）。
    // 12-V 复核修复：即使读不到 before 快照，也交给带持久化标记的通知器
    // 在历史基线已就绪时补发"同步已先行完成"的终态任务（尽力持久去重）。
    let notified = 0;
    if (result.refreshed) {
      try {
        notified = await createTaskNotifier(asyncStorageStore).notifyTransitions(
          before ?? [],
          await tasks.list(),
        );
      } catch {
        // 保留真实同步结果，不能因通知错误改写ran/online/refreshed。
      }
    }
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
