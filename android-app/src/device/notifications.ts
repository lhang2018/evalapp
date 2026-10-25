/**
 * 后台任务完成提醒（PRD 64）：只提醒状态跃迁到终态的任务。
 *
 * 纯 TS 便于单测；原生模块缺失时静默降级。通知是尽力而为，
 * 不改变任务状态，也不替代界面内的状态展示。
 *
 * 12-V 复核修复：后台同步可能在前台自动同步之后才运行，此时 `before`
 * 快照里任务已经是 DONE，"仅按本次 run 的 before/after 跃迁"会漏发。
 * 因此增加持久化投递标记：只要 after 中任务为 DONE 且本地标记不是 DONE，
 * 就补发一次并在投递被系统接受后写入标记（失败不写，留待下次重试）。
 */

import type { LocalTask } from "../offline/types.ts";

export const MAX_NOTIFICATION_TITLE_CHARS = 80;
export const MAX_NOTIFICATION_BODY_CHARS = 240;

export interface NativeNotificationModuleLike {
  /** true只表示系统接受本次投递请求，不证明用户已经收到或阅读。 */
  notifyTaskUpdate(id: number, title: string, body: string): Promise<boolean>;
}

/** 持久化"已投递"标记的最小存储接口（AsyncStorage 兼容）。 */
export interface NotificationMarkerStore {
  getItem(key: string): Promise<string | null>;
  setItem(key: string, value: string): Promise<void>;
}

export interface TaskNotification {
  id: number;
  title: string;
  body: string;
}

const NOTIFIED_PREFIX = "evoagent_task_notified:";

/** FNV-1a 31 位：同一任务的通知始终复用同一 ID，避免反复堆叠。 */
export function notificationIdFor(localTaskId: string): number {
  let hash = 0x811c9dc5;
  for (let i = 0; i < localTaskId.length; i += 1) {
    hash ^= localTaskId.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 1) & 0x7fffffff;
}

function collapse(value: unknown, maxChars: number): string {
  const text =
    typeof value === "string" ? value.replace(/\s+/g, " ").trim() : "";
  if (text.length <= maxChars) return text;
  return `${text.slice(0, Math.max(0, maxChars - 1))}…`;
}

export function describeTerminalTask(task: LocalTask): TaskNotification | null {
  if (task.status !== "DONE") return null;
  const status = task.server_status;
  if (status && !["SUCCEEDED", "FAILED", "CANCELLED"].includes(status)) return null;
  const failure = collapse(task.failure_reason, MAX_NOTIFICATION_BODY_CHARS);
  const result = collapse(task.result, MAX_NOTIFICATION_BODY_CHARS);
  const goal = collapse(task.goal, MAX_NOTIFICATION_BODY_CHARS);
  // 服务端终态优先；无server_status的旧记录/离线任务保留兼容推导。
  const cancelled = status === "CANCELLED";
  const failed = status === "FAILED" || (!status && Boolean(failure));
  return {
    id: notificationIdFor(task.local_task_id),
    title: collapse(
      cancelled ? "任务已取消" : failed ? "任务失败" : "任务完成",
      MAX_NOTIFICATION_TITLE_CHARS,
    ),
    body:
      cancelled ? failure || "任务已取消" : failed ? failure || "任务执行失败"
        : result || goal || "任务已完成",
  };
}

export class TaskNotifier {
  private readonly module: NativeNotificationModuleLike | null;
  private readonly store: NotificationMarkerStore | null;

  constructor(
    module: NativeNotificationModuleLike | null,
    store: NotificationMarkerStore | null = null,
  ) {
    this.module = module;
    this.store = store;
  }

  /** 只挑出本次刷新中从非终态进入 DONE 的任务；新增任务不补发提醒。 */
  completed(before: LocalTask[], after: LocalTask[]): LocalTask[] {
    const previous = new Map(
      before.map((task) => [task.local_task_id, task.status]),
    );
    return after.filter(
      (task) =>
        task.status === "DONE" &&
        previous.has(task.local_task_id) &&
        previous.get(task.local_task_id) !== "DONE",
    );
  }

  /** 持久化标记未写过的终态任务（覆盖"同步已先行完成"的窗口）。 */
  private async unmarkedTerminal(after: LocalTask[]): Promise<LocalTask[]> {
    if (!this.store) return [];
    const rows: LocalTask[] = [];
    for (const task of after) {
      if (task.status !== "DONE") continue;
      try {
        const marked = await this.store.getItem(
          NOTIFIED_PREFIX + task.local_task_id,
        );
        if (marked !== "DONE") rows.push(task);
      } catch {
        // 标记读取失败时退化为"仅本次跃迁"，不阻断同步
      }
    }
    return rows;
  }

  async notifyTransitions(
    before: LocalTask[],
    after: LocalTask[],
  ): Promise<number> {
    if (!this.module) return 0;
    const candidates = this.completed(before, after);
    const seen = new Set(candidates.map((task) => task.local_task_id));
    for (const task of await this.unmarkedTerminal(after)) {
      if (!seen.has(task.local_task_id)) {
        seen.add(task.local_task_id);
        candidates.push(task);
      }
    }
    let posted = 0;
    for (const task of candidates) {
      const notice = describeTerminalTask(task);
      if (!notice) continue;
      try {
        const accepted = await this.module.notifyTaskUpdate(
          notice.id,
          notice.title,
          notice.body,
        );
        if (accepted === true) {
          posted += 1;
          if (this.store) {
            try {
              await this.store.setItem(
                NOTIFIED_PREFIX + task.local_task_id,
                "DONE",
              );
            } catch {
              // 标记写入失败只影响去重，不影响本次投递
            }
          }
        }
      } catch {
        // 通知失败不影响同步与任务状态
      }
    }
    return posted;
  }
}
