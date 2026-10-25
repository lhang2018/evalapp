/**
 * 用户可见任务状态映射（ALN-23-D）。
 *
 * 单一权威映射：服务端 Task 状态机（task.py TaskStatus）与本地任务状态
 * （offline/types.ts LocalTaskStatus）→ 用户可见状态 + 等待原因。
 * 规则（任务卡 23-D）：
 * - 状态来自 Task/事件权威字段；模型/助手文字不能伪造完成或进度——
 *   只有服务端终态（或本地 DONE 且有 server 终态）才算完成；
 * - 暂停/待澄清/等待网络等等待原因必须可见；
 * - 展示措辞是产品语义，不改内部 Task 状态契约。
 */

import type { LocalTask } from "../../offline/types.ts";

export type ServerTaskStatus =
  | "CREATED" | "CLASSIFYING" | "PLANNING" | "READY" | "RUNNING"
  | "WAITING_USER" | "WAITING_DEPENDENCY" | "RECOVERING" | "VERIFYING"
  | "SUCCEEDED" | "FAILED" | "SUSPENDED" | "CANCELLED";

export type UserTaskState =
  | "queued"        // 已受理，尚未开始执行
  | "working"       // 执行中
  | "needs_you"     // 需要用户动作（澄清/审批/恢复确认）
  | "waiting"       // 系统等待（依赖/网络/调度），非用户动作
  | "done"          // 已完成（仅服务端终态可判定）
  | "failed"        // 失败（含取消前的失败语义）
  | "cancelled";    // 已取消

export interface TaskWaitReason {
  /** 等待原因（用户可见），无等待时为 null */
  reason: string | null;
  /** true = 需要用户动作才能继续（对应 needs_you） */
  needsUserAction: boolean;
}

export interface UserTaskView {
  state: UserTaskState;
  /** 用户可见文案（不含模型文字） */
  label: string;
  wait: TaskWaitReason;
}

const SERVER_STATE_VIEW: Record<ServerTaskStatus, {
  state: UserTaskState; label: string;
}> = {
  CREATED: { state: "queued", label: "已受理" },
  CLASSIFYING: { state: "working", label: "理解目标中" },
  PLANNING: { state: "working", label: "制定方案中" },
  READY: { state: "queued", label: "准备执行" },
  RUNNING: { state: "working", label: "执行中" },
  WAITING_USER: { state: "needs_you", label: "等待你的输入" },
  WAITING_DEPENDENCY: { state: "waiting", label: "等待前置条件" },
  RECOVERING: { state: "working", label: "恢复执行中" },
  VERIFYING: { state: "working", label: "验证结果中" },
  SUCCEEDED: { state: "done", label: "已完成" },
  FAILED: { state: "failed", label: "失败" },
  SUSPENDED: { state: "waiting", label: "已暂停" },
  CANCELLED: { state: "cancelled", label: "已取消" },
};

/** 是否服务端终态：完成与否只由它决定，模型文字无效。 */
export function isTerminalServerStatus(status: string): boolean {
  return status === "SUCCEEDED" || status === "FAILED"
    || status === "CANCELLED";
}

/** 等待原因：来自权威字段（审批/澄清请求/本地同步错误），不来自模型文字。 */
export function waitReasonOf(task: {
  server_status?: string;
  waiting_approvals?: string[];
  waiting_request?: { request_id?: string; question?: string } | null;
  last_sync_error?: string | null;
  status?: string;
}): TaskWaitReason {
  if (task.waiting_approvals && task.waiting_approvals.length) {
    return { reason: "等待你审批确认", needsUserAction: true };
  }
  if (task.waiting_request) {
    return { reason: "等待你补充信息", needsUserAction: true };
  }
  if (task.status === "NEEDS_RECONCILE") {
    return { reason: "需要你确认后重提（提交时被中断）", needsUserAction: true };
  }
  if (task.last_sync_error) {
    return { reason: "等待网络恢复后同步", needsUserAction: false };
  }
  if (task.server_status === "SUSPENDED") {
    return { reason: "任务已暂停，可继续或取消", needsUserAction: false };
  }
  if (task.server_status === "WAITING_DEPENDENCY") {
    return { reason: "等待前置任务完成", needsUserAction: false };
  }
  return { reason: null, needsUserAction: false };
}

/**
 * 本地+服务端状态 → 用户视图。server_status 为空（离线未提交）时按本地
 * 状态展示；完成只认服务端终态。
 */
export function taskViewOf(task: LocalTask): UserTaskView {
  const server = task.server_status as ServerTaskStatus | undefined;
  const wait = waitReasonOf(task);
  if (server && isTerminalServerStatus(server)) {
    const base = SERVER_STATE_VIEW[server];
    return { state: base.state, label: base.label, wait };
  }
  if (server && SERVER_STATE_VIEW[server]) {
    const base = SERVER_STATE_VIEW[server];
    // 等待原因覆盖默认文案（如 WAITING_USER 的具体原因）
    const label = wait.reason ?? base.label;
    const state = wait.reason && wait.needsUserAction ? "needs_you"
      : wait.reason && !wait.needsUserAction && base.state === "working"
        ? base.state
        : base.state;
    return { state, label, wait };
  }
  // 无服务端状态：本地阶段
  switch (task.status) {
    case "LOCAL_ONLY":
    case "SUBMITTING":
      return { state: "queued", label: "正在提交", wait };
    case "SUBMITTED":
      return { state: "queued", label: "已提交，等待服务端受理", wait };
    case "WAITING_DEPENDENCY":
      return { state: "waiting", label: "等待前置条件", wait };
    case "NEEDS_RECONCILE":
      return {
        state: "needs_you",
        label: "需要你确认后重提",
        wait: { reason: "提交时被中断，需确认后重提", needsUserAction: true },
      };
    case "DONE":
      // DONE 且无服务端终态：以本地结果为准展示，但不宣称服务端已验证
      return {
        state: task.result ? "done" : "failed",
        label: task.result ? "已完成（本地）" : "失败",
        wait,
      };
    default:
      return { state: "queued", label: task.status, wait };
  }
}
