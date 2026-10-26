/** 本地任务库：离线创建、幂等提交、状态刷新与崩溃恢复。 */

import type {
  LocalTask, LocalTaskStatus, OfflineState, TaskContractInput,
  UserInputRequest,
} from "./types.ts";
import type { OfflineRepository } from "./storage.ts";
import { createClientRequestId, createLocalTaskId } from "./ids.ts";
import { enqueueInto } from "./queue.ts";
import { runOffline } from "./policy.ts";

export interface TaskTransport {
  submitTask(
    goal: string,
    intent: string,
    riskLevel: string,
    clientRequestId: string,
    contract?: TaskContractInput | null,
  ): Promise<{ task_id: string }>;
  getTask(taskId: string): Promise<{
    current_run_id?: string | null;
    waiting_request?: UserInputRequest | null;
    waiting_approvals?: string[];
    task_status: string;
    result?: string | null;
    failure_reason?: string | null;
  }>;
}

export interface TaskFlushResult {
  submitted: number;
  failed: number;
  recovered: number;
}

export function mapServerStatus(status: string): LocalTaskStatus {
  if (status === "SUCCEEDED") return "DONE";
  if (status === "FAILED" || status === "CANCELLED") return "DONE";
  if (status === "WAITING_USER" || status === "WAITING_DEPENDENCY" ||
      status === "SUSPENDED") {
    return "WAITING_DEPENDENCY";
  }
  return "SUBMITTED";
}

function isTerminalServerStatus(status: string): boolean {
  return status === "SUCCEEDED" || status === "FAILED" ||
    status === "CANCELLED";
}

export class LocalTaskService {
  private running: Promise<TaskFlushResult> | null = null;
  private readonly repo: OfflineRepository;
  private readonly transport: TaskTransport;
  private readonly now: () => number;
  /** 客户端运行版本清单引用，随本机事件留痕 */
  private readonly runtimeManifestId: string | null;

  constructor(
    repo: OfflineRepository,
    transport: TaskTransport,
    now: () => number = () => Date.now(),
    runtimeManifestId: string | null = null,
  ) {
    this.repo = repo;
    this.transport = transport;
    this.now = now;
    this.runtimeManifestId = runtimeManifestId;
  }

  /** 离线可执行意图直接本地完成；其余进入 LOCAL_ONLY 等待上线。 */
  submit(goal: string, intent: string, riskLevel: string = "low",
         contract?: TaskContractInput | null): Promise<LocalTask> {
    const now = this.now();
    const offline = runOffline(goal, intent, new Date(now));
    return this.repo.update((state: OfflineState) => {
      const task: LocalTask = {
        local_task_id: createLocalTaskId(),
        client_request_id: createClientRequestId(),
        server_task_id: null,
        goal,
        intent,
        risk_level: riskLevel,
        contract: contract ?? null,
        status: offline ? "DONE" : "LOCAL_ONLY",
        result: offline?.result ?? null,
        failure_reason: null,
        created_at: now,
        updated_at: now,
        last_sync_error: null,
      };
      state.tasks.push(task);
      enqueueInto(state, {
        event_type: offline ? "task_completed_local" : "task_queued",
        task_id: task.local_task_id,
        runtime_manifest_id: this.runtimeManifestId,
        payload: { intent, risk_level: riskLevel, offline: Boolean(offline) },
      }, now);
      return task;
    });
  }

  async list(): Promise<LocalTask[]> {
    const state = await this.repo.load();
    return [...state.tasks].sort((a, b) => b.created_at - a.created_at);
  }

  async get(localTaskId: string): Promise<LocalTask | null> {
    const state = await this.repo.load();
    return state.tasks.find((t) => t.local_task_id === localTaskId) ?? null;
  }

  /** 启动时调用：崩溃时处于 SUBMITTING 的任务不能盲目重放。 */
  recoverInterrupted(): Promise<number> {
    const now = this.now();
    return this.repo.update((state) => {
      let count = 0;
      for (const task of state.tasks) {
        if (task.execution_target !== "device" && task.status === "SUBMITTING") {
          task.status = "NEEDS_RECONCILE";
          task.last_sync_error = "提交过程被中断，需确认是否已创建";
          task.updated_at = now;
          count += 1;
        }
      }
      return count;
    });
  }

  flushPending(): Promise<TaskFlushResult> {
    if (this.running) return this.running;
    this.running = this._flushPending().finally(() => {
      this.running = null;
    });
    return this.running;
  }

  private async _flushPending(): Promise<TaskFlushResult> {
    const state = await this.repo.load();
    const pending = state.tasks.filter((t) => t.execution_target !== "device" && t.status === "LOCAL_ONLY");
    const result: TaskFlushResult = { submitted: 0, failed: 0, recovered: 0 };
    for (const task of pending) {
      await this.setStatus(task.local_task_id, "SUBMITTING", null);
      try {
        const response = await this.transport.submitTask(
          task.goal,
          task.intent,
          task.risk_level,
          task.client_request_id,
          task.contract ?? null,
        );
        await this.repo.update((current) => {
          const target = current.tasks.find(
            (t) => t.local_task_id === task.local_task_id,
          );
          if (target) {
            target.status = "SUBMITTED";
            target.server_task_id = response.task_id;
            target.last_sync_error = null;
            target.updated_at = this.now();
          }
          try {
            enqueueInto(current, {
              event_type: "task_submitted",
              task_id: task.local_task_id,
              runtime_manifest_id: this.runtimeManifestId,
              payload: { server_task_id: response.task_id },
            }, this.now());
          } catch {
            // 队列满不应回滚已成功的提交
          }
        });
        result.submitted += 1;
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        // 幂等键保证重试安全，保持在 LOCAL_ONLY 等待下次联网
        await this.repo.update((current) => {
          const target = current.tasks.find(
            (t) => t.local_task_id === task.local_task_id,
          );
          if (target) {
            target.status = "LOCAL_ONLY";
            target.last_sync_error = message;
            target.updated_at = this.now();
          }
        });
        result.failed += 1;
      }
    }
    return result;
  }

  /** 轮询服务端状态；离线时保留原状态并记录错误。 */
  async refresh(): Promise<number> {
    const state = await this.repo.load();
    const targets = state.tasks.filter(
      (t) =>
        t.execution_target !== "device" &&
        (t.status === "SUBMITTED" || t.status === "WAITING_DEPENDENCY") &&
        Boolean(t.server_task_id),
    );
    let updated = 0;
    for (const task of targets) {
      try {
        const info = await this.transport.getTask(task.server_task_id as string);
        const mapped = mapServerStatus(info.task_status);
        await this.repo.update((current) => {
          const target = current.tasks.find(
            (t) => t.local_task_id === task.local_task_id,
          );
          if (!target) return;
          const changed = target.status !== mapped ||
            target.server_status !== info.task_status ||
            target.current_run_id !== info.current_run_id ||
            target.waiting_request?.request_id !== info.waiting_request?.request_id ||
            JSON.stringify(target.waiting_approvals ?? []) !== JSON.stringify(info.waiting_approvals ?? []) ||
            (info.result ?? null) !== target.result ||
            (info.failure_reason ?? null) !== target.failure_reason;
          target.status = mapped;
          target.server_status = info.task_status;
          target.current_run_id = info.current_run_id ?? null;
          target.waiting_request = info.waiting_request ?? null;
          target.waiting_approvals = info.waiting_approvals ?? [];
          target.result = info.result ?? null;
          target.failure_reason = info.failure_reason ?? null;
          target.last_sync_error = null;
          if (changed) {
            target.updated_at = this.now();
            updated += 1;
            if (isTerminalServerStatus(info.task_status)) {
              try {
                enqueueInto(current, {
                  event_type: "task_status_changed",
                  task_id: target.local_task_id,
                  runtime_manifest_id: this.runtimeManifestId,
                  payload: {
                    server_task_id: target.server_task_id,
                    status: info.task_status,
                  },
                }, this.now());
              } catch {
                // 队列满时忽略事件，不影响状态更新
              }
            }
          }
        });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        await this.repo.update((current) => {
          const target = current.tasks.find(
            (t) => t.local_task_id === task.local_task_id,
          );
          if (target) target.last_sync_error = message;
        });
      }
    }
    return updated;
  }

  /** 用户确认可安全重提（幂等键不变）。 */
  confirmResubmit(localTaskId: string): Promise<boolean> {
    return this.repo.update((state) => {
      const task = state.tasks.find((t) => t.local_task_id === localTaskId);
      if (!task || task.execution_target === "device" || task.status !== "NEEDS_RECONCILE") return false;
      task.status = "LOCAL_ONLY";
      task.last_sync_error = null;
      task.updated_at = this.now();
      return true;
    });
  }

  private setStatus(
    localTaskId: string,
    status: LocalTaskStatus,
    error: string | null,
  ): Promise<void> {
    return this.repo.update((state) => {
      const task = state.tasks.find((t) => t.local_task_id === localTaskId);
      if (!task) return;
      task.status = status;
      task.last_sync_error = error;
      task.updated_at = this.now();
    });
  }
}
