/** 设备执行闸门：风险分级、独立确认、动作审计日志、禁止重放未知副作用。 */

import {
  isPackageName,
  normalizeSnapshot,
  verifyAction,
} from "./observation.ts";
import type { DeviceTransport } from "./transport.ts";
import type {
  ActionJournalEntry,
  DeviceAction,
  DeviceResult,
  DeviceRisk,
  JournalState,
} from "./types.ts";

export type ConfirmFn = (
  action: DeviceAction,
  risk: DeviceRisk,
) => boolean | Promise<boolean>;

export interface DeviceBrokerOptions {
  confirm?: ConfirmFn;
  now?: () => number;
  maxConsecutiveUnverified?: number;
}

const HIGH_RISK_KINDS = new Set([
  "send_message",
  "payment",
  "delete",
  "call",
  "uninstall",
]);

function hasEvidence(evidence?: Record<string, unknown>): boolean {
  return Boolean(evidence) && Object.keys(evidence as object).length > 0;
}

function stateForStatus(status: DeviceResult["status"]): JournalState {
  if (status === "verified") return "verified";
  if (status === "failed") return "failed";
  if (status === "denied") return "denied";
  if (status === "unavailable") return "unavailable";
  return "unverified";
}

export class DeviceBroker {
  private readonly transport: DeviceTransport;
  private readonly confirm: ConfirmFn | null;
  private readonly now: () => number;
  private readonly maxConsecutiveUnverified: number;
  private readonly entries = new Map<string, ActionJournalEntry>();
  private consecutiveUnverified = 0;
  private stopped = false;

  constructor(transport: DeviceTransport, options: DeviceBrokerOptions = {}) {
    this.transport = transport;
    this.confirm = options.confirm ?? null;
    this.now = options.now ?? (() => Date.now());
    this.maxConsecutiveUnverified = options.maxConsecutiveUnverified ?? 3;
  }

  isStopped(): boolean {
    return this.stopped;
  }

  /** 人工恢复：仅在用户确认后调用，会清零连续失败计数。 */
  resume(): void {
    this.stopped = false;
    this.consecutiveUnverified = 0;
  }

  riskOf(action: DeviceAction): DeviceRisk {
    if (action.risk) return action.risk;
    if (HIGH_RISK_KINDS.has(action.kind)) return "high";
    if (action.kind === "click_text") return "medium";
    return "low";
  }

  async execute(action: DeviceAction): Promise<DeviceResult> {
    const risk = this.riskOf(action);
    const prior = this.entries.get(action.action_id);
    if (prior) return this.replayResult(prior);

    if (this.stopped) {
      const message =
        `安全停止：连续 ${this.consecutiveUnverified} 次设备操作未通过验证，需人工恢复`;
      this.record(action, risk, "denied", message);
      return { status: "denied", message };
    }

    let available = false;
    try {
      available = await this.transport.isAvailable();
    } catch {
      available = false;
    }
    if (!available) {
      const message = "无障碍服务不可用或未授权";
      this.record(action, risk, "unavailable", message);
      return { status: "unavailable", message };
    }

    if (risk === "high") {
      let allowed = false;
      if (this.confirm) {
        try {
          allowed = await this.confirm(action, risk);
        } catch {
          allowed = false;
        }
      }
      if (!allowed) {
        const message = "高风险操作未获用户确认";
        this.record(action, risk, "denied", message);
        return { status: "denied", message };
      }
    }

    this.record(action, risk, "prepared");
    this.record(action, risk, "dispatched");

    let result: DeviceResult;
    try {
      result = await this.transport.dispatch({ ...action, risk });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      result = { status: "failed", message };
    }

    if (result.status === "verified") {
      result = this.verifyResult(action, result);
    }
    if (result.status === "failed" || result.status === "unverified") {
      this.consecutiveUnverified += 1;
      if (this.consecutiveUnverified >= this.maxConsecutiveUnverified) {
        this.stopped = true;
      }
    } else if (result.status === "verified") {
      this.consecutiveUnverified = 0;
    }
    this.record(
      action,
      risk,
      stateForStatus(result.status),
      result.message,
      result.evidence,
    );
    return result;
  }

  private verifyResult(action: DeviceAction, result: DeviceResult): DeviceResult {
    if (!hasEvidence(result.evidence)) {
      return {
        status: "unverified",
        message: result.message ?? "执行结果缺少可验证证据",
      };
    }
    const evidence = result.evidence as Record<string, unknown>;
    const before = normalizeSnapshot(evidence.before);
    const after = normalizeSnapshot(evidence.after);
    if (!before || !after) return result;

    const expectation: { expect_text?: string; expect_package?: string } = {};
    const expectText = action.args?.expect_text;
    if (typeof expectText === "string" && expectText.trim()) {
      expectation.expect_text = expectText.trim();
    }
    if (action.kind === "launch_intent" && isPackageName(action.target)) {
      expectation.expect_package = action.target;
    }
    const verification = verifyAction(before, after, expectation);
    const merged = { ...evidence, page_change: verification.evidence };
    if (verification.status === "verified") {
      return {
        status: "verified",
        message: verification.message,
        evidence: merged,
      };
    }
    return {
      status: "unverified",
      message: verification.message,
      evidence: merged,
    };
  }

  listJournal(): ActionJournalEntry[] {
    return [...this.entries.values()].map((entry) => ({ ...entry }));
  }

  getJournal(actionId: string): ActionJournalEntry | null {
    const entry = this.entries.get(actionId);
    return entry ? { ...entry } : null;
  }

  private replayResult(entry: ActionJournalEntry): DeviceResult {
    if (entry.state === "verified") {
      return {
        status: "verified",
        evidence: entry.evidence,
        message: "动作已执行（幂等，未重复下发）",
      };
    }
    if (entry.state === "dispatched" ||
        entry.state === "unverified" ||
        entry.state === "prepared") {
      return {
        status: "unverified",
        message: "动作已下发但结果未确认，禁止自动重放",
      };
    }
    if (entry.state === "denied") {
      return { status: "denied", message: entry.message ?? "动作已被拒绝" };
    }
    if (entry.state === "unavailable") {
      return {
        status: "unavailable",
        message: entry.message ?? "无障碍服务不可用",
      };
    }
    return {
      status: "failed",
      message: entry.message ?? "动作上次执行失败，未自动重放",
    };
  }

  private record(
    action: DeviceAction,
    risk: DeviceRisk,
    state: JournalState,
    message?: string,
    evidence?: Record<string, unknown>,
  ): void {
    const now = this.now();
    const existing = this.entries.get(action.action_id);
    const entry: ActionJournalEntry = existing ?? {
      action_id: action.action_id,
      kind: action.kind,
      risk,
      state,
      created_at: now,
      updated_at: now,
    };
    entry.state = state;
    entry.updated_at = now;
    entry.message = message;
    if (evidence) entry.evidence = evidence;
    this.entries.set(action.action_id, entry);
  }
}
