/** 把 EvoAgentClient 适配为离线层传输契约；网络错误向上抛出由队列退避处理。 */

import type {
  EventSyncTransport,
  SyncResponse,
} from "./sync.ts";
import type { TaskTransport } from "./tasks.ts";
import type { OfflineEvent } from "./types.ts";

interface HttpLike {
  syncEvents(events: unknown[]): Promise<SyncResponse>;
  submitTask(
    goal: string,
    intent: string,
    riskLevel: string,
    clientRequestId: string,
  ): Promise<{ task_id: string }>;
  getTask(taskId: string): Promise<{
    task_status: string;
    result?: string | null;
    failure_reason?: string | null;
  }>;
}

export class ClientTransport implements EventSyncTransport, TaskTransport {
  private readonly client: HttpLike;

  constructor(client: HttpLike) {
    this.client = client;
  }

  syncEvents(events: OfflineEvent[]): Promise<SyncResponse> {
    return this.client.syncEvents(
      events.map((event) => ({
        event_id: event.event_id,
        event_type: event.event_type,
        task_id: event.task_id ?? undefined,
        // 本地用毫秒（Date.now）；服务端事件时间统一为秒
        timestamp:
          event.timestamp > 1e12 ? event.timestamp / 1000 : event.timestamp,
        severity: event.severity,
        payload: event.payload,
        privacy_classification: event.privacy_classification,
      })),
    );
  }

  submitTask(
    goal: string,
    intent: string,
    riskLevel: string,
    clientRequestId: string,
  ) {
    return this.client.submitTask(
      goal,
      intent,
      riskLevel,
      clientRequestId,
    );
  }

  getTask(taskId: string) {
    return this.client.getTask(taskId);
  }
}
