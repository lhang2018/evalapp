/**
 * 项目空间与跨会话延续（ALN-24-C）。
 *
 * 把多个任务的 goal/constraints/产物/结果按项目（goal_id 或用户自定义
 * 分组）归集，支持跨会话恢复已确认的工作状态。
 *
 * 规则（任务卡验收）：
 * - 不建立第二套 Task 状态——引用既有 local_task_id / server_task_id；
 * - 项目是任务的组织容器，不是新的状态机；
 * - 跨会话延续 = 从持久化存储重建项目上下文（goal/约束/产物清单）。
 */

import type { LocalTask } from "../../offline/types.ts";

export interface ProjectSpace {
  project_id: string;
  /** 项目名称（来自 goal_id 或用户输入） */
  name: string;
  /** 归集的任务 ID 列表（引用既有 LocalTask，不复制状态） */
  task_ids: string[];
  created_at: number;
  updated_at: number;
}

export interface ProjectSummary {
  project_id: string;
  name: string;
  task_count: number;
  /** 已完成任务数 */
  done_count: number;
  /** 进行中任务数 */
  active_count: number;
  /** 项目内全部约束（去重合并） */
  constraints: string[];
  /** 最近更新时间 */
  updated_at: number;
}

export interface ContinuationContext {
  project_id: string;
  /** 可恢复的目标 */
  goals: string[];
  /** 已确认约束（跨任务合并去重） */
  constraints: string[];
  /** 已有产物清单（跨任务合并） */
  artifacts: Array<{ task_id: string; node_id: string; artifact_id: string }>;
  /** 未完成任务 */
  openTasks: Array<{ task_id: string; goal: string; status: string }>;
}

/**
 * 从任务列表按 goal_id（或 intent）归集项目空间。
 * 不修改任务本身——只做只读归集。
 */
export function groupByProject(
  tasks: LocalTask[],
): Map<string, LocalTask[]> {
  const groups = new Map<string, LocalTask[]>();
  for (const t of tasks) {
    const key = t.goal_id ?? t.intent ?? "ungrouped";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(t);
  }
  return groups;
}

/** 从任务组生成项目摘要。 */
export function projectSummary(
  project_id: string, name: string, tasks: LocalTask[],
): ProjectSummary {
  const constraints = new Set<string>();
  let done = 0, active = 0, latest = 0;
  for (const t of tasks) {
    for (const c of t.contract?.constraints ?? []) constraints.add(c);
    if (t.status === "DONE") done += 1;
    else active += 1;
    if (t.updated_at > latest) latest = t.updated_at;
  }
  return {
    project_id, name,
    task_count: tasks.length,
    done_count: done,
    active_count: active,
    constraints: [...constraints],
    updated_at: latest,
  };
}

/**
 * 跨会话延续上下文：从持久化任务重建项目工作状态。
 * 只引用已有 LocalTask 数据，不创建新状态。
 */
export function continuationContext(
  project_id: string, tasks: LocalTask[],
): ContinuationContext {
  const goals = new Set<string>();
  const constraints = new Set<string>();
  const artifacts: ContinuationContext["artifacts"] = [];
  const openTasks: ContinuationContext["openTasks"] = [];

  for (const t of tasks) {
    if (t.goal) goals.add(t.goal);
    for (const c of t.contract?.constraints ?? []) constraints.add(c);
    for (const e of t.result_evidence ?? []) {
      if (typeof e === "object" && e !== null
          && "artifact_id" in e && "node_id" in e) {
        artifacts.push({
          task_id: t.local_task_id,
          node_id: String(e.node_id),
          artifact_id: String(e.artifact_id),
        });
      }
    }
    if (t.status !== "DONE") {
      openTasks.push({ task_id: t.local_task_id, goal: t.goal,
                       status: t.status });
    }
  }

  return {
    project_id,
    goals: [...goals],
    constraints: [...constraints],
    artifacts,
    openTasks,
  };
}
