/** 任务通知原生模块的安全封装：缺失模块时返回空操作。 */

import { NativeModules } from "react-native";

import { TaskNotifier } from "./notifications.ts";
import type { NativeNotificationModuleLike } from "./notifications.ts";

export function createTaskNotifier(): TaskNotifier {
  const module = (NativeModules as Record<string, unknown>)
    .EvoAgentNotifications as NativeNotificationModuleLike | null | undefined;
  return new TaskNotifier(module ?? null);
}
