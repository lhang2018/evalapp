/**
 * 任务工作区投影（ALN-23-C）：把 Task/Run/Node/Checkpoint 权威数据投影为
 * 用户可见的工作区视图——计划节点依赖、真实阶段、用户待办、部分成果。
 *
 * 规则（任务卡验收）：
 * - 六种节点态可辨（pending/running/succeeded/failed/skipped/blocked）；
 * - 阶段有权威记录（来自 checkpoint/runtime_manifest，非模型文字）；
 * - 无依据不显示百分比（只有全部节点有终态才显示 100%）；
 * - 部分成果和待授权可见。
 */

import type { LocalTask } from "../../offline/types.ts";

// ---- 节点状态 ----

export type NodeState =
  | "pending"
  | "running"
  | "succeeded"
  | "failed"
  | "skipped"
  | "blocked";

export interface WorkspaceNode {
  node_id: string;
  tool: string;
  state: NodeState;
  deps: string[];
  output?: unknown;
  error?: string;
}

// ---- 工作区视图 ----

export interface WorkspaceView {
  task_id: string;
  goal: string;
  status: string;
  nodes: WorkspaceNode[];
  /** 已完成节点数（含 skipped） */
  completedCount: number;
  totalCount: number;
  /** 仅当全部节点有终态时才显示 100%；否则 null（无依据不显示百分比） */
  percent: number | null;
  /** 用户待办 */
  pendingActions: PendingAction[];
  /** 部分成果（已完成节点的输出） */
  partialResults: Array<{ node_id: string; output: unknown }>;
  /** 失败节点 */
  failures: Array<{ node_id: string; error: string }>;
}

export interface PendingAction {
  kind: "approval" | "clarification" | "reconcile" | "budget";
  reason: string;
  ref_id: string;
}

// ---- 从 checkpoint 快照投影 ----

export interface CheckpointSnapshot {
  workflow_outputs?: Record<string, unknown>;
  pending_nodes?: string[];
  completed_nodes?: string[];
  budget_state?: { usage?: Record<string, unknown> };
}

/**
 * 从 checkpoint 快照 + 权威任务状态构建工作区视图。
 * checkpoint 是权威记录（写 SQLite 时由 runs.freeze 落盘），
 * 不是模型文字。
 */
export function workspaceFromCheckpoint(
  task: LocalTask,
  checkpoint: CheckpointSnapshot | null,
  nodeDefs: Array<{ node_id: string; tool: string; deps?: string[] }> = [],
): WorkspaceView {
  const completed = new Set(checkpoint?.completed_nodes ?? []);
  const pending = new Set(checkpoint?.pending_nodes ?? []);
  const outputs = checkpoint?.workflow_outputs ?? {};

  const nodes: WorkspaceNode[] = nodeDefs.map((def) => {
    let state: NodeState = "pending";
    if (completed.has(def.node_id)) state = "succeeded";
    else if (pending.has(def.node_id)) state = "pending";
    // blocked 仅由服务端权威数据判定；客户端不基于 deps 推断
    return {
      node_id: def.node_id, tool: def.tool, state,
      deps: def.deps ?? [],
      output: outputs[def.node_id],
    };
  });

  const succeeded = nodes.filter((n) => n.state === "succeeded");
  const failed = nodes.filter((n) => n.state === "failed");
  const terminal = succeeded.length + failed.length +
    nodes.filter((n) => n.state === "skipped").length;
  // 无依据不显示百分比：只有全部节点有终态才显示
  const percent = nodes.length > 0 && terminal === nodes.length
    ? Math.round((succeeded.length / nodes.length) * 100)
    : null;

  // 用户待办
  const pendingActions: PendingAction[] = [];
  if (task.waiting_approvals?.length) {
    for (const aid of task.waiting_approvals) {
      pendingActions.push({
        kind: "approval", reason: "需要你审批确认", ref_id: aid,
      });
    }
  }
  if (task.waiting_request && !task.waiting_request.answered) {
    pendingActions.push({
      kind: "clarification",
      reason: task.waiting_request.question ?? "需要你补充信息",
      ref_id: task.waiting_request.request_id ?? "",
    });
  }
  if (task.status === "NEEDS_RECONCILE") {
    pendingActions.push({
      kind: "reconcile", reason: "提交被中断，需确认后重提",
      ref_id: task.local_task_id,
    });
  }

  const partialResults = succeeded
    .filter((n) => n.output != null)
    .map((n) => ({ node_id: n.node_id, output: n.output }));
  const failures = failed.map((n) => ({
    node_id: n.node_id, error: n.error ?? "未知错误",
  }));

  return {
    task_id: task.local_task_id,
    goal: task.goal,
    status: task.status,
    nodes,
    completedCount: succeeded.length,
    totalCount: nodes.length,
    percent,
    pendingActions,
    partialResults,
    failures,
  };
}
