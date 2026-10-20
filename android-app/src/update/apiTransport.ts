/** 把 EvoAgentClient 适配为更新层传输契约（双端协同 §5.2）。 */

import type {
  ApplyResultRequest,
  ReconcileRequest,
  ReconcileResponse,
  UpdateTransport,
} from "./types.ts";

interface HttpLike {
  reconcile(req: ReconcileRequest): Promise<ReconcileResponse>;
  applyUpdateResult(
    req: ApplyResultRequest,
  ): Promise<{ ok: boolean; duplicate?: boolean }>;
}

export class ApiUpdateTransport implements UpdateTransport {
  private readonly client: HttpLike;

  constructor(client: HttpLike) {
    this.client = client;
  }

  reconcile(req: ReconcileRequest): Promise<ReconcileResponse> {
    return this.client.reconcile(req);
  }

  applyResult(
    req: ApplyResultRequest,
  ): Promise<{ ok: boolean; duplicate?: boolean }> {
    return this.client.applyUpdateResult(req);
  }
}
