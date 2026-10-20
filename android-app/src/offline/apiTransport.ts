/** 把 EvoAgentClient 适配为离线层传输契约；网络错误向上抛出由队列退避处理。 */

import type {
  EventSyncTransport,
  SyncResponse,
} from "./sync.ts";
import type { TaskTransport } from "./tasks.ts";
import type { OfflineEvent } from "./types.ts";
import { createId } from "./ids.ts";

interface SyncMeta {
  batch_id?: string;
  client_id?: string;
  installation_id?: string;
}

interface TaskMeta {
  device_id?: string;
  installation_id?: string;
  app_version?: string;
}

interface HttpLike {
  syncEvents(events: unknown[], meta?: SyncMeta): Promise<SyncResponse>;
  submitTask(
    goal: string,
    intent: string,
    riskLevel: string,
    clientRequestId: string,
    meta?: TaskMeta,
  ): Promise<{ task_id: string }>;
  getTask(taskId: string): Promise<{
    task_status: string;
    result?: string | null;
    failure_reason?: string | null;
  }>;
}

export interface ClientTransportOptions {
  installationId?: string;
  deviceId?: string;
  appVersion?: string;
  clientId?: string;
}

export class ClientTransport implements EventSyncTransport, TaskTransport {
  private readonly client: HttpLike;
  private installationId: string | null;
  private deviceId: string | null;
  private appVersion: string | null;
  private readonly clientId: string;

  constructor(client: HttpLike, options: ClientTransportOptions = {}) {
    this.client = client;
    this.installationId = options.installationId ?? null;
    this.deviceId = options.deviceId ?? null;
    this.appVersion = options.appVersion ?? null;
    this.clientId = options.clientId ?? "android";
  }

  /** 安装实例标识在首次启动时生成，随后回填到事件批次与任务提交。 */
  setDeviceInfo(info: {
    installationId?: string;
    deviceId?: string;
    appVersion?: string;
  }): void {
    if (info.installationId) this.installationId = info.installationId;
    if (info.deviceId) this.deviceId = info.deviceId;
    if (info.appVersion) this.appVersion = info.appVersion;
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
      {
        // 每批独立批次号；event_id 才是服务端权威去重键
        batch_id: createId("batch"),
        client_id: this.clientId,
        installation_id: this.installationId ?? undefined,
      },
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
      {
        device_id: this.deviceId ?? undefined,
        installation_id: this.installationId ?? undefined,
        app_version: this.appVersion ?? undefined,
      },
    );
  }

  getTask(taskId: string) {
    return this.client.getTask(taskId);
  }
}
