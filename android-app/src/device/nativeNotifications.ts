/** 任务通知原生模块的安全封装：缺失模块时返回空操作。 */

import { NativeModules } from "react-native";

import { TaskNotifier } from "./notifications.ts";
import type {
  NativeNotificationModuleLike,
  NotificationMarkerStore,
} from "./notifications.ts";

/**
 * @param store 可选持久化标记存储（用于跨 run 补发去重）；
 *              不传时保持"仅按本次 before/after 跃迁"的旧语义。
 */
export function createTaskNotifier(
  store: NotificationMarkerStore | null = null,
): TaskNotifier {
  const module = (NativeModules as Record<string, unknown>)
    .EvoAgentNotifications as NativeNotificationModuleLike | null | undefined;
  return new TaskNotifier(module ?? null, store);
}
