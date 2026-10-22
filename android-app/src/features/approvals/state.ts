import type { ApprovalInfo, EvoAgentClient } from "../../api.ts";

export type ApprovalTransport = Pick<EvoAgentClient, "getApproval" | "decideApproval" | "getTask">;
export interface ApprovalState {
  detail?: ApprovalInfo;
  busy: boolean;
  verified: boolean;
  error: string;
  notice: string;
  taskStatus?: string;
}

const messages: Record<number, string> = {
  401: "管理员凭证无效或已失效，请重新输入。",
  403: "当前凭证没有管理员审批权限。",
  404: "审批记录不存在，请刷新任务。",
  409: "审批状态已变化，请刷新后查看结果。",
};

/** Session-only state. Credentials and server error bodies never enter persisted state. */
export class ApprovalSession {
  state: ApprovalState = { busy: false, verified: false, error: "", notice: "" };
  private token = "";
  private generation = 0;
  private online = true;
  private transport: ApprovalTransport;
  readonly id: string;
  readonly taskId: string;
  private changed: (state: ApprovalState) => void;
  constructor(transport: ApprovalTransport, id: string, taskId: string,
              changed: (state: ApprovalState) => void = () => {}) {
    this.transport = transport; this.id = id; this.taskId = taskId; this.changed = changed;
  }

  configure(token: string, online: boolean) {
    if (token !== this.token || online !== this.online) {
      this.generation++;
      this.token = token;
      this.online = online;
      this.publish({ busy: false, verified: false, error: "", notice: "" });
    }
  }

  dispose() { this.generation++; this.token = ""; }
  private publish(patch: Partial<ApprovalState>) {
    this.state = { ...this.state, ...patch };
    this.changed(this.state);
  }
  private fail(error: unknown) {
    const status = (error as { status?: number } | null)?.status;
    this.publish({ verified: false, error: messages[status ?? 0] ??
      "网络请求未确认。请刷新本条审批查看状态，再决定是否重试。" });
  }
  private async read(token: string) {
    const detail = await this.transport.getApproval(this.id, token);
    if (detail.approval_id !== this.id || detail.task_id !== this.taskId)
      throw new Error("approval identity mismatch");
    return detail;
  }
  canDecide(now = Date.now()) {
    const d = this.state.detail;
    return this.online && !!this.token.trim() && this.state.verified && !this.state.busy &&
      d?.status === "pending" && !!d.binding_json && !!d.action_digest &&
      d.expires_at != null && d.expires_at * 1000 > now;
  }
  async refresh() {
    if (this.state.busy || !this.online || !this.token.trim()) return;
    const generation = this.generation, token = this.token;
    this.publish({ busy: true, verified: false, error: "" });
    try {
      const detail = await this.read(token);
      if (generation === this.generation) this.publish({ detail, verified: true });
      if (generation !== this.generation) return;
      const task = await this.transport.getTask(this.taskId, token);
      if (generation === this.generation) this.publish({ taskStatus: task.task_status });
    } catch (e) { if (generation === this.generation) this.fail(e); }
    finally { if (generation === this.generation) this.publish({ busy: false }); }
  }
  async decide(approved: boolean) {
    if (!this.canDecide()) return;
    const generation = this.generation, token = this.token;
    const shownDigest = this.state.detail!.action_digest;
    this.publish({ busy: true, verified: false, error: "", notice: "" });
    try {
      const fresh = await this.read(token);
      if (generation !== this.generation) return;
      this.publish({ detail: fresh });
      if (fresh.status !== "pending" || fresh.action_digest !== shownDigest ||
          !fresh.expires_at || fresh.expires_at * 1000 <= Date.now()) {
        this.publish({ verified: true, notice: "审批内容或状态已变化，请查看最新详情。" });
        return;
      }
      const result = await this.transport.decideApproval(this.id, approved, token);
      if (generation !== this.generation) return;
      this.publish({ notice: result.accepted ? "决定已受理，任务执行结果仍需刷新确认。" : "决定已记录。" });
      const detail = await this.read(token);
      if (generation !== this.generation) return;
      this.publish({ detail, verified: true });
      const task = await this.transport.getTask(this.taskId, token);
      if (generation === this.generation) this.publish({ taskStatus: task.task_status });
    } catch (e) { if (generation === this.generation) this.fail(e); }
    finally { if (generation === this.generation) this.publish({ busy: false }); }
  }
}

export function approvalStatus(detail?: ApprovalInfo, now = Date.now()): string {
  if (!detail) return "尚未读取";
  if (detail.status === "pending" && detail.expires_at != null && detail.expires_at * 1000 <= now)
    return "已到期（以服务端为准）";
  return ({ pending: "待审批", approved: "已批准，等待执行", used: "已消费", denied: "已拒绝",
    revoked: "已撤销", expired: "已过期" } as Record<string, string>)[detail.status] ?? detail.status;
}

export function fullBinding(detail: ApprovalInfo): string {
  if (!detail.binding_json) return "缺少绑定动作，无法审批。";
  try { return JSON.stringify(JSON.parse(detail.binding_json), null, 2); }
  catch { return detail.binding_json; }
}
