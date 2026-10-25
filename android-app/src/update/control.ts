/**
 * 更新控制与回滚证据视图（ALN-29-E，UX-28/GLOBAL-01）。
 *
 * 只做投影与守卫决策，不改更新管理器状态机：
 * - 稳定/实验版本可理解：channel + phase 投影为用户可读的版本角色；
 * - GLOBAL-01 运行绑定不可无约束热切换：存在运行中任务时激活被拒
 *   （ADR-012：运行期间固定组件版本）；App 的 activationAllowed
 *   钩子消费本判定，manager 状态机不变；
 * - 拒绝可见：check/reasons/phase 的用户可读映射，坏包拒绝不再
 *   只是一行内部错误；
 * - 回滚证据绑定同一版本：rolled_back 回执携带 version/digest，
 *   投影为「从哪个版本回滚到哪个版本」的配对证据。
 */

import type { UpdateState } from "./types.ts";

export type VersionRole = "stable" | "candidate" | "unknown";

export interface UpdateControlView {
  /** 当前激活版本的角色（channel 判定；无激活槽 = unknown） */
  currentRole: VersionRole;
  currentVersion: string | null;
  /** 候选版本角色（无候选 = null） */
  candidateRole: VersionRole | null;
  candidateVersion: string | null;
  /** GLOBAL-01：是否存在阻止激活的运行绑定因素 */
  activationBlockedReasons: string[];
  /** 用户可读的最近拒绝原因（坏包/不兼容等） */
  rejectionNotice: string | null;
  /** 回滚证据：from→to 同绑定的版本对（无回滚历史 = null） */
  rollbackEvidence: {
    fromVersion: string;
    toVersion: string;
    digest: string | null;
    at: number;
  } | null;
}

const EXPERIMENTAL_CHANNELS = new Set([
  "experimental", "canary", "beta", "dev",
]);

function roleOf(channel: string | undefined | null): VersionRole {
  if (!channel) return "unknown";
  return EXPERIMENTAL_CHANNELS.has(channel.toLowerCase())
    ? "candidate"
    : "stable";
}

/** 运行中本地任务计数（App 传入），GLOBAL-01 的守卫输入。 */
export function canActivateNow(
  state: UpdateState,
  runningTasks: number,
): { allowed: boolean; reasons: string[] } {
  const reasons: string[] = [];
  if (state.phase !== "verified" || !state.pending) {
    reasons.push("当前没有已验证待激活的候选版本");
  }
  if (runningTasks > 0) {
    reasons.push(
      `有 ${runningTasks} 个任务正在运行；运行绑定期间禁止热切换版本` +
      "（等任务结束或暂停后再激活）");
  }
  return { allowed: reasons.length === 0, reasons };
}

export function updateControlView(state: UpdateState): UpdateControlView {
  const active = state.slots[state.active_slot];
  // 已激活槽即当前稳定轨（11-P 语义）；候选角色由 target.channel 判定
  const currentRole: VersionRole = active ? "stable" : "unknown";
  const candidateChannel = state.target?.channel ?? null;

  // 最近一次回执类拒绝（坏包/不兼容）对用户可见
  const rejectionNotice =
    state.phase === "failed" && state.last_error
      ? state.last_error
      : state.phase === "blocked" && state.reasons.length
        ? state.reasons.join("；")
        : null;

  // 回滚证据：最近一条 rolled_back 回执 + 目标版本（同绑定）
  const rolledBack = [...state.receipts]
    .reverse()
    .find((r) => r.action === "rolled_back");
  const rollbackEvidence = rolledBack
    ? {
      fromVersion: rolledBack.version ?? "unknown",
      toVersion: rolledBack.to_release_id ?? rolledBack.release_id,
      digest: rolledBack.artifact_digest ?? null,
      at: rolledBack.last_attempt_at,
    }
    : null;

  return {
    currentRole: active ? currentRole : "unknown",
    currentVersion: active?.version ?? null,
    candidateRole: candidateChannel
      ? roleOf(candidateChannel)
      : state.pending
        ? "candidate"
        : null,
    candidateVersion: state.pending?.version ?? state.target?.version ?? null,
    activationBlockedReasons: [],
    rejectionNotice,
    rollbackEvidence,
  };
}
