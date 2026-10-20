/**
 * EvoAgent REST API 客户端。
 * 对应服务端 evoagent/api/server.py 的接口契约：
 *  POST /tasks            {goal, intent, risk_level}
 *  GET  /tasks            → {tasks: TaskInfo[]}
 *  GET  /tasks/{id}       → TaskInfo
 *  GET  /tasks/{id}/events → {events: RunEvent[]}
 *  POST /tasks/{id}/cancel
 *  GET  /health
 */
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
  ) {
    const body: Record<string, unknown> = {
      goal,
      intent,
      risk_level: riskLevel,
    };
    if (clientRequestId) body.client_request_id = clientRequestId;
    return this.req<{ task_id: string; deduplicated?: boolean }>("/tasks", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  syncEvents(events: unknown[]) {
    return this.req<{
      accepted: string[];
      duplicates: string[];
      rejected: { event_id: string; error: string }[];
    }>("/events/sync", {
      method: "POST",
      body: JSON.stringify({ client_id: "android", events }),
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
}

export function api(baseUrl: string) {
  return new EvoAgentClient(baseUrl);
}
