/**
 * 用户控制路径（ALN-23-E）：暂停/取消/继续/目标变更与安全恢复。
 *
 * 原则（任务卡验收）：
 * - 授权不随目标扩大（用户控制动作走既有权限/确认语义）；
 * - 未知副作用先对账、不盲目重放（复用 02-A/04-R 语义）；
 * - 暂停/取消/继续是权威操作（走 controller），客户端只展示结果；
 * - 目标变更是新提交（不修改已有 Task）——旧任务保留历史。
 */

import type { LocalTask } from "../../offline/types.ts";
import { taskViewOf } from "../taskview/state.ts";

// ---- 可用控制动作 ----

export type ControlAction = "pause" | "cancel" | "resume" | "confirm_reconcile";

export interface ControlOption {
  action: ControlAction;
  /** 用户可见标签 */
  label: string;
  /** 是否需要用户确认 */
  needsConfirm: boolean;
  /** 确认提示 */
  confirmHint: string;
}

/**
 * 根据任务当前状态返回可用的控制动作列表。
 * 只读投影——实际执行走 services.tasks / services.controller。
 */
export function availableControls(task: LocalTask): ControlOption[] {
  const view = taskViewOf(task);
  const options: ControlOption[] = [];
  if (view.state === "done" || view.state === "cancelled" || view.state === "failed") {
    return options;
  }
  // 取消：非终态都可取消
  options.push({
    action: "cancel", label: "取消任务",
    needsConfirm: true, confirmHint: "取消后不可恢复，确认取消？",
  });
  // 暂停：执行中可暂停
  if (view.state === "working") {
    options.push({
      action: "pause", label: "暂停任务",
      needsConfirm: false, confirmHint: "",
    });
  }
  // 继续：暂停中的可继续
  if (view.state === "waiting" && view.label.includes("暂停")) {
    options.push({
      action: "resume", label: "继续任务",
      needsConfirm: false, confirmHint: "",
    });
  }
  // 确认重提：NEEDS_RECONCILE
  if (task.status === "NEEDS_RECONCILE") {
    options.push({
      action: "confirm_reconcile", label: "确认重提",
      needsConfirm: true, confirmHint: "上次提交被中断；确认重提将创建新任务",
    });
  }
  return options;
}

// ---- 目标变更：创建新任务（不修改已有 Task） ----

export interface GoalChangePlan {
  /** 旧任务 ID（保留历史） */
  oldTaskId: string;
  /** 新目标文本 */
  newGoal: string;
  /** 变更原因（用户可见） */
  reason: string;
  /** true = 目标变更后需要重新走权限/预算审批 */
  requiresReapproval: boolean;
}

/**
 * 目标变更计划：不修改已有 Task，创建新提交。
 * 权限/预算按新任务独立审批——授权不随目标扩大。
 */
export function planGoalChange(
  oldGoal: string, newGoal: string,
): GoalChangePlan {
  const changed = oldGoal.trim() !== newGoal.trim() && newGoal.trim().length > 0;
  return {
    oldTaskId: "",
    newGoal: newGoal.trim(),
    reason: changed ? "目标已变更，将创建新任务" : "目标未变更",
    requiresReapproval: changed,
  };
}

// ---- 安全恢复：未知副作用先对账 ----

export interface RecoveryCheck {
  /** true = 可以安全恢复 */
  safe: boolean;
  /** 阻塞原因（不可恢复时给用户看） */
  blocker: string | null;
  /** 需要对账的副作用列表 */
  pendingEffects: Array<{ effect_id: string; status: string }>;
}

/**
 * 恢复前安全检查：有未决副作用（unknown/pending）时阻断恢复，
 * 要求先对账——不盲目重放。
 */
export function checkRecoverySafety(
  sideEffects: Array<{ effect_id: string; status: string }>,
): RecoveryCheck {
  const terminal = new Set(["confirmed", "failed", "compensated"]);
  const pending = sideEffects.filter((e) => !terminal.has(e.status));
  if (pending.length > 0) {
    return {
      safe: false,
      blocker: `有 ${pending.length} 个未决副作用需要先对账`,
      pendingEffects: pending,
    };
  }
  return { safe: true, blocker: null, pendingEffects: [] };
}
