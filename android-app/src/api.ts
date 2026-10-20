/**
 * EvoAgent REST API 客户端。
 * 对应服务端 evoagent/api/server.py 的接口契约：
 *  POST /tasks            {goal, intent, risk_level, client_request_id?}
 *  GET  /tasks            → {tasks: TaskInfo[]}
 *  GET  /tasks/{id}       → TaskInfo
 *  GET  /tasks/{id}/events → {events: RunEvent[]}
 *  POST /tasks/{id}/cancel
 *  POST /events/sync
 *  POST /sync/reconcile
 *  GET  /sync/candidates
 *  POST /sync/apply-result
 *  GET  /health
 */

import type {
  ApplyResultRequest,
  CandidateSyncResponse,
  ReconcileRequest,
  ReconcileResponse,
} from "./update/types.ts";

export interface TaskInfo {
  task_id: string;
  user_goal: string;
  intent: string;
  domain: string;
  task_status: string;
  result?: string | null;
  failure_reason?: string | null;
  result_evidence?: unknown[];
  created_at: number;
}

export interface RunEvent {
  event_id: string;
  event_type: string;
  task_id?: string;
  severity: string;
  timestamp: number;
  payload: Record<string, unknown>;
}

export interface MetricsSnapshot {
  tasks: {
    total: number;
    by_status: Record<string, number>;
    success_rate: number | null;
    avg_latency_s: number | null;
    model_calls: number;
    tool_calls: number;
    cost: number;
    failure_by_intent: Record<string, number>;
  };
  diagnostics: { cases: number };
  evolution: { candidates: Record<string, number>; datasets: string[] };
  budget: Record<string, Record<string, number>>;
  releases: {
    current: string | null;
    count: number;
    deployments: number;
    rollbacks: number;
  };
  tools_health: Record<string, unknown>;
}

export interface CandidateSummary {
  candidate_id: string;
  status: string;
  risk_level: string;
  target_component: string;
  target_problem: string;
  publish_suggestion?: string | null;
  eval_dataset_id?: string | null;
  created_at: number;
}

// ---- PRD 89.3 工作流中心 ----
export interface WorkflowSummary {
  workflow_id: string;
  version: string;
  status: string;
  updated_at: number;
  definition: Record<string, unknown>;
}

export interface WorkflowVersion {
  version: string;
  status: string;
  updated_at: number;
}

export interface WorkflowRun {
  task_id: string;
  status: string;
  user_goal: string;
  created_at: number;
  updated_at: number;
  result?: string | null;
  failure_reason?: string | null;
  workflow_version?: string | null;
  completed_nodes: string[];
  pending_nodes: string[];
  artifacts: Record<string, unknown>;
}

export interface WorkflowCenterDetail {
  workflow: Record<string, unknown>;
  versions: WorkflowVersion[];
  runs: WorkflowRun[];
}

// ---- PRD 89.4 记忆中心 ----
export interface MemoryEntry {
  memory_id: string;
  kind: string;
  scope: string;
  scope_key: string;
  status: string;
  content: string;
  source: string;
  applicable_domains: string[];
  superseded_by?: string | null;
  valid_until?: number | null;
  created_at: number;
  updated_at: number;
  use_count: number;
}

// ---- PRD 89.6 / 89.8 设置与更新 ----
export interface ModelInfo {
  model_id: string;
  provider: string;
  context_limit: number;
  supports_tool_call: boolean;
  supports_vision: boolean;
  price_per_1k_input: number;
  price_per_1k_output: number;
  quality_score: number;
  avg_latency_s: number;
  available: boolean;
  compat_tested: boolean;
  last_verified_at: number;
  known_limitations: string[];
}

export interface ModelsInfo {
  default_model_id: string;
  routes: Record<string, unknown>;
  models: ModelInfo[];
}

export interface ReleaseManifest {
  release_id: string;
  version: string;
  code_commit: string;
  model_adapter_version: string;
  workflow_versions: string[];
  rollback_target?: string | null;
  created_at: number;
}

export interface ReleasesInfo {
  current: string | null;
  manifests: ReleaseManifest[];
  deployments: Record<string, unknown>[];
  rollbacks: Record<string, unknown>[];
}

export interface ReleaseVerify {
  ok: boolean;
  [key: string]: unknown;
}

export interface VersionInfo {
  release_id: string;
  version: string;
  code_commit: string;
  model_adapter_version: string;
  workflow_versions: string[];
  created_at: number;
  current: string | null;
}

export class EvoAgentClient {
  private base: string;

  constructor(baseUrl: string) {
    this.base = baseUrl.replace(/\/+$/, "");
  }

  private async req<T>(path: string, init?: RequestInit): Promise<T> {
    const resp = await fetch(this.base + path, {
      headers: { "Content-Type": "application/json" },
      ...init,
    });
    const body = await resp.json();
    if (!resp.ok && body?.error) throw new Error(String(body.error));
    return body as T;
  }

  health() {
    return this.req<{ status: string; provider: string }>("/health");
  }

  listTasks(): Promise<TaskInfo[]> {
    return this.req<{ tasks: TaskInfo[] }>("/tasks").then((r) => r.tasks);
  }

  getTask(id: string) {
    return this.req<TaskInfo>(`/tasks/${id}`);
  }

  events(id: string): Promise<RunEvent[]> {
    return this.req<{ events: RunEvent[] }>(`/tasks/${id}/events`)
      .then((r) => r.events);
  }

  async submitTask(
    goal: string,
    intent: string,
    riskLevel = "low",
    clientRequestId?: string,
    meta: {
      device_id?: string;
      installation_id?: string;
      app_version?: string;
    } = {},
  ) {
    const body: Record<string, unknown> = {
      goal,
      intent,
      risk_level: riskLevel,
    };
    if (clientRequestId) body.client_request_id = clientRequestId;
    if (meta.device_id) body.device_id = meta.device_id;
    if (meta.installation_id) body.installation_id = meta.installation_id;
    if (meta.app_version) body.app_version = meta.app_version;
    return this.req<{ task_id: string; deduplicated?: boolean }>("/tasks", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  syncEvents(
    events: unknown[],
    meta: {
      batch_id?: string;
      client_id?: string;
      installation_id?: string;
    } = {},
  ) {
    const body: Record<string, unknown> = {
      client_id: meta.client_id ?? "android",
      events,
    };
    if (meta.batch_id) body.batch_id = meta.batch_id;
    if (meta.installation_id) body.installation_id = meta.installation_id;
    return this.req<{
      accepted: string[];
      duplicates: string[];
      rejected: { event_id: string; error: string }[];
      receipt?: { receipt_id: string; batch_id: string; replayed: boolean };
    }>("/events/sync", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  /** 版本对账（双端协同 §5.2）：服务端返回目标版本与兼容性检查明细。 */
  reconcile(req: ReconcileRequest): Promise<ReconcileResponse> {
    return this.req<ReconcileResponse>("/sync/reconcile", {
      method: "POST",
      body: JSON.stringify(req),
    });
  }

  /** 候选阶段同步（双端协同 §2）：只读展示，不触发激活。 */
  syncCandidates(installationId: string): Promise<CandidateSyncResponse> {
    return this.req<CandidateSyncResponse>(
      `/sync/candidates?installation_id=${encodeURIComponent(installationId)}`,
    );
  }

  /** 上报更新应用回执（下载/校验/激活/健康检查/回滚）。 */
  applyUpdateResult(req: ApplyResultRequest) {
    return this.req<{
      ok: boolean;
      duplicate?: boolean;
      installation?: Record<string, unknown>;
    }>("/sync/apply-result", {
      method: "POST",
      body: JSON.stringify(req),
    });
  }

  cancelTask(id: string) {
    return this.req<{ ok: boolean }>(`/tasks/${id}/cancel`, { method: "POST" });
  }

  metrics() {
    return this.req<MetricsSnapshot>("/metrics");
  }

  candidates(limit = 10): Promise<CandidateSummary[]> {
    return this.req<{ candidates: CandidateSummary[] }>(
      `/evolution/candidates?limit=${limit}`,
    ).then((r) => r.candidates);
  }

  // ---- PRD 89.3 工作流中心 ----
  workflows(): Promise<WorkflowSummary[]> {
    return this.req<{ workflows: WorkflowSummary[] }>("/workflows")
      .then((r) => r.workflows);
  }

  workflow(id: string): Promise<WorkflowCenterDetail> {
    return this.req<WorkflowCenterDetail>(
      `/workflows/${encodeURIComponent(id)}`,
    );
  }

  // ---- PRD 89.4 记忆中心 ----
  async memory(filter: {
    kind?: string;
    scope?: string;
    status?: string;
    limit?: number;
  } = {}): Promise<MemoryEntry[]> {
    // RN 的 URLSearchParams.set 未实现，手动拼接查询串
    const params: string[] = [];
    if (filter.kind) params.push(`kind=${encodeURIComponent(filter.kind)}`);
    if (filter.scope) params.push(`scope=${encodeURIComponent(filter.scope)}`);
    if (filter.status) params.push(`status=${encodeURIComponent(filter.status)}`);
    params.push(`limit=${filter.limit ?? 100}`);
    const out = await this.req<{ items: MemoryEntry[] }>(
      `/memory?${params.join("&")}`,
    );
    return out.items;
  }

  correctMemory(memoryId: string, content: string, reason = "") {
    return this.req<{ item: MemoryEntry }>(
      `/memory/${encodeURIComponent(memoryId)}/correct`,
      { method: "POST", body: JSON.stringify({ content, reason }) },
    );
  }

  deleteMemory(memoryId: string) {
    return this.req<{ ok: boolean; memory_id: string }>(
      `/memory/${encodeURIComponent(memoryId)}`,
      { method: "DELETE" },
    );
  }

  // ---- PRD 89.6 / 89.8 设置与更新 ----
  modelsInfo() {
    return this.req<ModelsInfo>("/models");
  }

  releases() {
    return this.req<ReleasesInfo>("/releases");
  }

  releaseVerify(releaseId: string) {
    return this.req<{ manifest: ReleaseManifest; verify: ReleaseVerify }>(
      `/releases/${encodeURIComponent(releaseId)}`,
    );
  }

  version() {
    return this.req<VersionInfo>("/version");
  }
}

export function api(baseUrl: string) {
  return new EvoAgentClient(baseUrl);
}
