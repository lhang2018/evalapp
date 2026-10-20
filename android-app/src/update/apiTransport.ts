/** 把 EvoAgentClient 适配为更新层传输契约（双端协同 §5.2）。 */

import type {
  ApplyResultRequest,
  CandidateSyncResponse,
  ReconcileRequest,
  ReconcileResponse,
  UpdateTransport,
} from "./types.ts";

interface HttpLike {
  reconcile(req: ReconcileRequest): Promise<ReconcileResponse>;
  syncCandidates(installationId: string): Promise<CandidateSyncResponse>;
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

  candidateStates(installationId: string): Promise<CandidateSyncResponse> {
    return this.client.syncCandidates(installationId);
  }

  applyResult(
    req: ApplyResultRequest,
  ): Promise<{ ok: boolean; duplicate?: boolean }> {
    return this.client.applyUpdateResult(req);
  }
}
