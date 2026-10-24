/**
 * 自然语言任务 → 设备动作 提议/回执契约（ALN-12-A）。
 *
 * 职责：把任务链路（offline LocalTask）与设备执行闸门（DeviceBroker）用
 * 显式引用连起来：task_id → run_id → proposal → action_id → effect_id →
 * 回执（终态 + 证据 + 时间戳）。首份交付只定协议与台账，App 主链路接线
 * 按后续小卡推进。
 *
 * 引用规则：
 * - task_id：客户端本地任务标识（LocalTask.local_task_id），离线创建即存在，
 *   不依赖服务端分配。
 * - run_id：动作归属的运行；任务尚无服务端 run 时用 `local:<task_id>`，
 *   有 LocalTask.current_run_id 时用它。
 * - action_id：DeviceBroker 幂等键。同一 action_id 在同一 task 下重放
 *   返回原回执、永不重复下发；重试必须分配新 action_id（新提议）。
 * - effect_id：本次动作的副作用账本身份（预记账引用，对齐 ALN-02）。
 *   一经分配不可变更；回执与上传载荷都携带它。
 *
 * 边界：
 * - 只做协议与记录，不改变 broker 既有风险分级/确认/安全停止语义。
 * - 敏感内容默认不进入上传载荷：上传投影只含引用、状态与证据存在性，
 *   参数、目标文本、消息与证据内容本地保留（见 failureMatrix 各条目的
 *   local_retention）。
 */

import { createId } from "../offline/ids.ts";
import { sha256Hex } from "../offline/sha256.ts";
import { defaultRiskOf } from "./broker.ts";
import { isPackageName } from "./observation.ts";
import type { KeyValueStore } from "../offline/storage.ts";
import type {
  ActionJournalEntry,
  DeviceAction,
  DeviceResult,
  DeviceRisk,
  JournalState,
} from "./types.ts";

const riskOrder: DeviceRisk[] = ["low", "medium", "high"];
function effectiveRisk(kind: string, requested?: DeviceRisk): DeviceRisk {
  if (requested !== undefined && !riskOrder.includes(requested)) throw new Error("非法风险等级");
  return riskOrder[Math.max(riskOrder.indexOf(defaultRiskOf(kind)), riskOrder.indexOf(requested ?? "low"))];
}
function clone<T>(value: T): T { return JSON.parse(JSON.stringify(value)) as T; }
function fingerprint(value: unknown): string {
  const sort = (v: any): any => Array.isArray(v) ? v.map(sort)
    : v && typeof v === "object" ? Object.fromEntries(Object.keys(v).sort().map(k => [k, sort(v[k])])) : v;
  return sha256Hex(JSON.stringify(sort(value)));
}
function ownProposal(value: DeviceActionProposal): DeviceActionProposal {
  const p = clone(value);
  for (const key of ["proposal_id", "task_id", "run_id", "action_id", "effect_id", "kind", "target"] as const) {
    if (typeof p[key] !== "string" || !p[key].trim()) throw new Error(`非法提议字段: ${key}`);
  }
  if (!Number.isFinite(p.created_at)) throw new Error("非法提议时间");
  p.risk = effectiveRisk(p.kind, p.risk);
  return p;
}

// ---- 引用与提议契约 ----

/** 预期观察：执行后的可验证证据期望（映射 broker 前后快照校验）。 */
export interface ExpectedObservation {
  expect_package?: string;
  expect_text?: string;
  /** 要求页面必须发生变化才算验证通过 */
  page_change_required?: boolean;
}

export interface DeviceActionProposal {
  proposal_id: string;
  task_id: string;
  run_id: string;
  action_id: string;
  effect_id: string;
  kind: string;
  /** 目标应用/包名/路径/文本；上传时文本类目标脱敏 */
  target: string;
  /** 纯数据参数；整段本地保留，不进入上传载荷 */
  args?: Record<string, unknown>;
  risk: DeviceRisk;
  expected_observation?: ExpectedObservation;
  created_at: number;
}

export interface ProposalInput {
  task_id: string;
  run_id?: string | null;
  kind: string;
  target: string;
  args?: Record<string, unknown>;
  /** 缺省按 broker 同表推导（send_message/payment/delete/call/uninstall=high） */
  risk?: DeviceRisk;
  expected_observation?: ExpectedObservation;
  now?: number;
}

export function createProposal(input: ProposalInput): DeviceActionProposal {
  if (!input.task_id?.trim()) {
    throw new Error("设备动作提议缺少 task_id");
  }
  if (!input.kind?.trim()) {
    throw new Error("设备动作提议缺少动作类型");
  }
  if (!input.target?.trim()) {
    throw new Error("设备动作提议缺少目标");
  }
  const createdAt = input.now ?? Date.now();
  return {
    proposal_id: createId("prop"),
    task_id: input.task_id,
    run_id: input.run_id?.trim() || `local:${input.task_id}`,
    action_id: createId("act"),
    effect_id: createId("fx"),
    kind: input.kind,
    target: input.target,
    args: input.args === undefined ? undefined : clone(input.args),
    risk: effectiveRisk(input.kind, input.risk),
    expected_observation: input.expected_observation === undefined ? undefined : clone(input.expected_observation),
    created_at: createdAt,
  };
}

/** 提议 → broker 动作：预期观察映射为既有验证参数。 */
export function proposalToAction(
  proposal: DeviceActionProposal,
): DeviceAction {
  const args: Record<string, unknown> = { ...(proposal.args ?? {}) };
  if (proposal.expected_observation?.expect_text) {
    args.expect_text = proposal.expected_observation.expect_text;
  }
  if (proposal.expected_observation?.expect_package) args.expect_package = proposal.expected_observation.expect_package;
  if (proposal.expected_observation?.page_change_required) args.page_change_required = true;
  return {
    action_id: proposal.action_id,
    kind: proposal.kind,
    target: proposal.target,
    args,
    risk: proposal.risk,
    task_id: proposal.task_id,
    run_id: proposal.run_id,
    effect_id: proposal.effect_id,
  };
}

// ---- 回执契约 ----

/** 终态：不会再有后续状态。unverified 属结果不确定，须对账而非自动重放。 */
export const TERMINAL_JOURNAL_STATES: readonly JournalState[] = [
  "verified",
  "failed",
  "denied",
  "unavailable",
];

export function isTerminalJournalState(state: JournalState): boolean {
  return TERMINAL_JOURNAL_STATES.includes(state);
}

export interface ActionReceipt {
  effect_id: string;
  action_id: string;
  proposal_id: string;
  task_id: string;
  run_id: string;
  kind: string;
  risk: DeviceRisk;
  state: JournalState;
  terminal: boolean;
  /** 本次结果是否来自幂等重放（未重复下发） */
  replayed: boolean;
  message?: string;
  /** 证据快照（前后节点/页面变化），本地保留，上传只报存在性 */
  evidence?: Record<string, unknown>;
  occurred_at: number;
  recorded_at: number;
}

export function receiptFromJournal(
  proposal: DeviceActionProposal,
  entry: ActionJournalEntry,
  options: { replayed: boolean; recordedAt: number },
): ActionReceipt {
  if (entry.action_id !== proposal.action_id || entry.kind !== proposal.kind
      || entry.risk !== proposal.risk || (entry.task_id && entry.task_id !== proposal.task_id)
      || (entry.run_id && entry.run_id !== proposal.run_id)
      || (entry.effect_id && entry.effect_id !== proposal.effect_id)) {
    throw new Error("动作日志与提议身份不一致");
  }
  return {
    effect_id: proposal.effect_id,
    action_id: entry.action_id,
    proposal_id: proposal.proposal_id,
    task_id: proposal.task_id,
    run_id: proposal.run_id,
    kind: entry.kind,
    risk: entry.risk,
    state: entry.state,
    terminal: isTerminalJournalState(entry.state),
    replayed: options.replayed,
    message: entry.message,
    evidence: entry.evidence,
    occurred_at: entry.updated_at,
    recorded_at: options.recordedAt,
  };
}

// ---- 上传投影（敏感内容默认不出设备） ----

export interface ReceiptUploadProjection {
  effect_id: string;
  action_id: string;
  proposal_id: string;
  task_id: string;
  run_id: string;
  kind: string;
  risk: DeviceRisk;
  state: JournalState;
  terminal: boolean;
  replayed: boolean;
  has_evidence: boolean;
  occurred_at: number;
}

export function receiptForUpload(receipt: ActionReceipt): ReceiptUploadProjection {
  return {
    effect_id: receipt.effect_id,
    action_id: receipt.action_id,
    proposal_id: receipt.proposal_id,
    task_id: receipt.task_id,
    run_id: receipt.run_id,
    kind: receipt.kind,
    risk: receipt.risk,
    state: receipt.state,
    terminal: receipt.terminal,
    replayed: receipt.replayed,
    has_evidence: Boolean(
      receipt.evidence && Object.keys(receipt.evidence).length > 0),
    occurred_at: receipt.occurred_at,
  };
}

export interface ProposalUploadProjection {
  proposal_id: string;
  task_id: string;
  run_id: string;
  action_id: string;
  effect_id: string;
  kind: string;
  risk: DeviceRisk;
  /** package = 目标是包名/Intent，可上传；text = 屏幕文本，本地保留 */
  target_kind: "package" | "text";
  target: string;
  expected_observation?: ExpectedObservation;
  created_at: number;
}

export function proposalForUpload(
  proposal: DeviceActionProposal,
): ProposalUploadProjection {
  const textual = proposal.kind !== "launch_intent" || !isPackageName(proposal.target);
  const observation = proposal.expected_observation;
  return {
    proposal_id: proposal.proposal_id,
    task_id: proposal.task_id,
    run_id: proposal.run_id,
    action_id: proposal.action_id,
    effect_id: proposal.effect_id,
    kind: proposal.kind,
    risk: proposal.risk,
    target_kind: textual ? "text" : "package",
    target: textual ? "<local:text>" : proposal.target,
    expected_observation: observation ? {
      ...(observation.expect_package && isPackageName(observation.expect_package)
        ? { expect_package: observation.expect_package } : {}),
      ...(observation.page_change_required ? { page_change_required: true } : {}),
    } : undefined,
    created_at: proposal.created_at,
  };
}

/** 本地保留字段清单：契约要求与矩阵 local_retention 对齐。 */
export function localRetentionFields(proposal: DeviceActionProposal): string[] {
  const fields = ["args", "message", "evidence"];
  if (proposal.kind !== "launch_intent" || !isPackageName(proposal.target)) fields.push("target");
  if (proposal.expected_observation?.expect_text) fields.push("expected_observation.expect_text");
  return fields;
}

// ---- 任务动作台账（持久化；重启后引用与回执不丢失） ----

const LEDGER_KEY = "device_task_ledger_v1";
const DEFAULT_LEDGER_LIMIT = 200;

export interface TaskActionLedgerState {
  version: 1;
  proposals: Record<string, DeviceActionProposal>;
  receipts: Record<string, ActionReceipt>;
  /** Compact durable deduplication markers survive full-history retention. */
  retired?: Record<string, { fingerprint: string; receipt: ActionReceipt }>;
}

export function emptyLedgerState(): TaskActionLedgerState {
  return { version: 1, proposals: {}, receipts: {} };
}

function parseLedgerState(raw: string | null): TaskActionLedgerState {
  if (!raw) return emptyLedgerState();
  try {
    const data = JSON.parse(raw) as Partial<TaskActionLedgerState>;
    if (!data || data.version !== 1 || !data.proposals || !data.receipts
        || Array.isArray(data.proposals) || Array.isArray(data.receipts)
        || typeof data.proposals !== "object" || typeof data.receipts !== "object") throw new Error("invalid ledger");
    const state: TaskActionLedgerState = {
      version: 1,
      proposals: data.proposals ?? {},
      receipts: data.receipts ?? {},
      retired: data.retired ?? {},
    };
    if (!state.retired || typeof state.retired !== "object" || Array.isArray(state.retired)) throw new Error("invalid retirement markers");
    for (const [id, p] of Object.entries(state.proposals)) {
      ownProposal(p);
      if (p.action_id !== id) throw new Error("proposal key mismatch");
    }
    for (const [id, receipt] of Object.entries(state.receipts)) {
      const p = state.proposals[id];
      if (!p || !receipt || receipt.action_id !== id || receipt.task_id !== p.task_id
          || receipt.run_id !== p.run_id || receipt.effect_id !== p.effect_id
          || receipt.proposal_id !== p.proposal_id || receipt.kind !== p.kind
          || receipt.terminal !== isTerminalJournalState(receipt.state)) throw new Error("receipt identity mismatch");
    }
    for (const [id, marker] of Object.entries(state.retired)) {
      if (!marker || !/^[a-f0-9]{64}$/.test(marker.fingerprint) || !marker.receipt
          || marker.receipt.action_id !== id || !marker.receipt.terminal) throw new Error("invalid retired receipt");
    }
    return state;
  } catch {
    throw new Error("设备动作台账损坏，禁止清空后重新执行");
  }
}

const storeLocks = new WeakMap<KeyValueStore, Map<string, Promise<unknown>>>();
async function storeExclusive<T>(kv: KeyValueStore, key: string, fn: () => Promise<T>): Promise<T> {
  let locks = storeLocks.get(kv);
  if (!locks) { locks = new Map(); storeLocks.set(kv, locks); }
  const previous = locks.get(key) ?? Promise.resolve();
  const next = previous.catch(() => {}).then(fn);
  locks.set(key, next);
  try { return await next; } finally { if (locks.get(key) === next) locks.delete(key); }
}

export interface TaskActionRef {
  proposal: DeviceActionProposal;
  receipt: ActionReceipt | null;
}

export class TaskActionLedger {
  private readonly kv: KeyValueStore;
  private readonly now: () => number;
  private readonly storeKey: string;
  private readonly limit: number;

  constructor(
    kv: KeyValueStore,
    options: { now?: () => number; storeKey?: string; limit?: number } = {},
  ) {
    this.kv = kv;
    this.now = options.now ?? (() => Date.now());
    this.storeKey = options.storeKey ?? LEDGER_KEY;
    this.limit = options.limit ?? DEFAULT_LEDGER_LIMIT;
    if (!Number.isInteger(this.limit) || this.limit < 1) throw new Error("非法台账容量");
  }

  async loadState(): Promise<TaskActionLedgerState> {
    return parseLedgerState(await this.kv.getItem(this.storeKey));
  }

  async recordProposal(proposal: DeviceActionProposal): Promise<void> {
    proposal = ownProposal(proposal);
    await this.persist((state) => {
      this.assertIdentity(state, proposal);
      if (state.proposals[proposal.action_id] || state.retired?.[proposal.action_id]) return;
      state.proposals[proposal.action_id] = proposal;
    });
  }

  async getReceipt(actionId: string): Promise<ActionReceipt | null> {
    const state = await this.loadState();
    return state.receipts[actionId] ?? state.retired?.[actionId]?.receipt ?? null;
  }

  async getProposal(actionId: string): Promise<DeviceActionProposal | null> {
    const state = await this.loadState();
    return state.proposals[actionId] ?? null;
  }

  async recordReceipt(receipt: ActionReceipt): Promise<void> {
    receipt = clone(receipt);
    await this.persist((state) => {
      const p = state.proposals[receipt.action_id];
      if (!p || receipt.task_id !== p.task_id || receipt.run_id !== p.run_id
          || receipt.effect_id !== p.effect_id || receipt.proposal_id !== p.proposal_id) throw new Error("回执引用不一致");
      state.receipts[receipt.action_id] = receipt;
    });
  }

  async claimProposal(proposal: DeviceActionProposal): Promise<{ claimed: boolean; proposal: DeviceActionProposal; receipt: ActionReceipt | null }> {
    const p = ownProposal(proposal);
    return this.persist((state) => {
      this.assertIdentity(state, p);
      const existing = state.proposals[p.action_id], retired = state.retired?.[p.action_id];
      if (existing || retired) return { claimed: false, proposal: existing ?? p, receipt: state.receipts[p.action_id] ?? retired?.receipt ?? null };
      state.proposals[p.action_id] = p;
      return { claimed: true, proposal: p, receipt: null };
    });
  }

  async recordRecoveryReceipt(receipt: ActionReceipt): Promise<ActionReceipt> {
    return this.persist((state) => {
      const prior = state.receipts[receipt.action_id] ?? state.retired?.[receipt.action_id]?.receipt;
      if (prior) return { ...prior, replayed: true };
      state.receipts[receipt.action_id] = clone(receipt);
      return receipt;
    });
  }

  private assertIdentity(state: TaskActionLedgerState, p: DeviceActionProposal) {
    const existing = state.proposals[p.action_id];
    const expected = existing ? fingerprint(existing) : state.retired?.[p.action_id]?.fingerprint;
    if (expected && expected !== fingerprint(p)) throw new Error("action_id已绑定其他提议，禁止改写身份或参数");
  }

  /** 同一任务下的全部提议与回执（task → action → effect 链）。 */
  async listForTask(taskId: string): Promise<TaskActionRef[]> {
    const state = await this.loadState();
    return Object.values(state.proposals)
      .filter((p) => p.task_id === taskId)
      .sort((a, b) => a.created_at - b.created_at)
      .map((proposal) => ({
        proposal,
        receipt: state.receipts[proposal.action_id] ?? null,
      }));
  }

  private async persist<T>(
    mutate: (state: TaskActionLedgerState) => T,
  ): Promise<T> {
    return storeExclusive(this.kv, this.storeKey, async () => {
    const state = await this.loadState();
    const result = mutate(state);
    // 容量上界：按提议时间淘汰最旧条目，回执随提议一起淘汰。
    const ids = Object.values(state.proposals)
      .sort((a, b) => a.created_at - b.created_at);
    while (ids.length > this.limit) {
      const index = ids.findIndex(p => state.receipts[p.action_id]?.terminal);
      if (index < 0) throw new Error("台账包含未决动作，容量已满，禁止丢弃后继续执行");
      const oldest = ids.splice(index, 1)[0];
      state.retired ??= {};
      const receipt = state.receipts[oldest.action_id];
      state.retired[oldest.action_id] = { fingerprint: fingerprint(oldest), receipt: {
        ...receipt, evidence: undefined, message: undefined,
      } };
      delete state.proposals[oldest.action_id];
      delete state.receipts[oldest.action_id];
    }
    await this.kv.setItem(this.storeKey, JSON.stringify(state));
    return result;
    });
  }
}

// ---- 执行器：提议 → broker → 回执 ----

export interface ProposalExecution {
  receipt: ActionReceipt;
  /** 本次是否真实下发（false = 幂等重放原回执） */
  dispatched: boolean;
  /** broker 原始结果；仅本地使用，上传用 receiptForUpload 投影 */
  result?: DeviceResult;
}

/**
 * 执行设备动作提议（预记账：先落提议再下发）：
 * 1. 台账已有该 action_id 的回执 → 原样返回（replayed=true），不进 broker，
 *    不重复下发；
 * 2. 台账已有该 action_id 的提议但无回执 = 上次执行在落账与下发之间
 *    被中断（进程崩溃窗口），副作用未知 → 不自动重发，回执按
 *    unverified 留痕（terminal=false，可见、需人工对账）；
 * 3. 否则经 broker 既有授权/确认/验证路径执行一次，回执落台账。
 * 重试语义：任何已回执的 action_id 都不自动重发；重试 = 新提议新 action_id。
 * 未知副作用（dispatched/unverified）因此永不自动重放。
 */
export async function executeProposal(
  ledger: TaskActionLedger,
  broker: DeviceBrokerLike,
  proposal: DeviceActionProposal,
  options: { now?: () => number } = {},
): Promise<ProposalExecution> {
  const now = options.now ?? (() => Date.now());
  const claim = await ledger.claimProposal(proposal);
  proposal = claim.proposal;
  if (claim.receipt) {
    return { receipt: { ...claim.receipt, replayed: true }, dispatched: false };
  }
  if (!claim.claimed) {
    const unknown: ActionReceipt = {
      effect_id: proposal.effect_id,
      action_id: proposal.action_id,
      proposal_id: proposal.proposal_id,
      task_id: proposal.task_id,
      run_id: proposal.run_id,
      kind: proposal.kind,
      risk: proposal.risk,
      state: "unverified",
      terminal: false,
      replayed: false,
      message: "中断恢复：已记账无回执，副作用未知，需人工对账",
      occurred_at: now(),
      recorded_at: now(),
    };
    return { receipt: await ledger.recordRecoveryReceipt(unknown), dispatched: false };
  }
  const result = await broker.execute(proposalToAction(proposal));
  const entry = broker.getJournal(proposal.action_id);
  const receipt: ActionReceipt = entry
    ? receiptFromJournal(proposal, entry, {
      replayed: false,
      recordedAt: now(),
    })
    : {
      effect_id: proposal.effect_id,
      action_id: proposal.action_id,
      proposal_id: proposal.proposal_id,
      task_id: proposal.task_id,
      run_id: proposal.run_id,
      kind: proposal.kind,
      risk: proposal.risk,
      state: "unverified",
      terminal: false,
      replayed: false,
      message: result.message ?? "动作日志缺失，副作用待对账",
      occurred_at: now(),
      recorded_at: now(),
    };
  await ledger.recordReceipt(receipt);
  return { receipt, dispatched: true, result };
}

/** broker 最小接口（便于测试注入；真实用法传 DeviceBroker 实例）。 */
export interface DeviceBrokerLike {
  execute(action: DeviceAction): Promise<DeviceResult>;
  getJournal(actionId: string): ActionJournalEntry | null;
}
