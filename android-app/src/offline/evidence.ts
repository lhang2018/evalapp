/**
 * 端侧 EvidenceEvent 投影（ALN-16-P）。
 *
 * 把本地 OfflineEvent 投影为与服务端 observability 契约对齐的证据事件
 * 形态（origin=device / trust 按来源 / 契约四级隐私 / fault 推断）。
 * 本模块只做投影与脱敏，不改上传链路——队列同步仍走既有 privacy
 * 过滤（仅 standard 上传）；本投影供后续证据上报与决策链对齐消费。
 *
 * 隐私规则与服务端 repository.py 同口径：
 * - sensitive 事件（设备动作/位置/记忆写入）投影 restricted + redacted，
 *   正文不复制只留引用；
 * - 未知分级 fail-safe 到 restricted；
 * - 含疑似密钥的载荷直接拒绝（不入证据）。
 */

import type { OfflineEvent } from "./types.ts";

export type ContractPrivacy = "public" | "internal" | "sensitive" | "restricted";
export type ContractFault = "none" | "transient" | "degraded" | "fatal";

export interface EvidenceRef {
  task_id?: string;
  run_id?: string;
  effect_id?: string;
  approval_id?: string;
}

export interface ClientEvidenceEvent {
  schema_version: "evoagent.evidence/1";
  event_id: string;
  event_type: string;
  timestamp: number;
  origin: "device";
  trust: "trusted" | "untrusted";
  untrusted_origin: string;
  privacy_level: ContractPrivacy;
  fault_level: ContractFault;
  severity: OfflineEvent["severity"];
  component_id: string;
  refs: EvidenceRef;
  /** restricted 时为空对象（只留 payload_ref） */
  payload: Record<string, unknown>;
  payload_ref: string | null;
  redacted: boolean;
}

const SENSITIVE_EVENT_TYPES = new Set([
  "device_action",
  "device_observation",
  "memory_write",
  "location_update",
]);

const SECRET_PATTERNS = [
  /sk-[A-Za-z0-9]{16,}/,
  /(?:api_?key|token|secret|password)["':=]\s*\S{8,}/i,
];

function normalizePrivacy(level: string): ContractPrivacy {
  if (level === "standard" || level === "internal") return "internal";
  if (level === "public") return "public";
  if (level === "sensitive") return "sensitive";
  // secret/private/未知：fail-safe 收紧
  return "restricted";
}

function inferFault(event: OfflineEvent): ContractFault {
  const name = event.event_type.toLowerCase();
  if (event.severity === "critical" || name.includes("fatal")) return "fatal";
  if (event.severity === "error") return "degraded";
  if (event.severity === "warn") return "transient";
  if (name.includes("unknown")) return "degraded";
  return "none";
}

function payloadText(payload: Record<string, unknown>): string {
  try {
    return JSON.stringify(payload) ?? "";
  } catch {
    return "";
  }
}

export function containsSecretPayload(
  payload: Record<string, unknown>,
): boolean {
  const text = payloadText(payload);
  return SECRET_PATTERNS.some((pattern) => pattern.test(text));
}

/**
 * OfflineEvent → 契约对齐证据事件。敏感类型 restricted+redacted；
 * 含疑似密钥抛错（调用方不得将其纳入任何证据流）。
 */
export function toEvidenceEvent(event: OfflineEvent): ClientEvidenceEvent {
  const sensitive = SENSITIVE_EVENT_TYPES.has(event.event_type);
  let privacy = normalizePrivacy(event.privacy_classification);
  const failSafe = privacy === "restricted" && !sensitive;
  const restricted = sensitive || failSafe;
  if (containsSecretPayload(event.payload)) {
    throw new Error(
      `事件载荷疑似包含密钥，拒绝入证据流: ${event.event_id}`);
  }
  return {
    schema_version: "evoagent.evidence/1",
    event_id: event.event_id,
    event_type: event.event_type,
    timestamp: Math.floor(event.timestamp / 1000),
    origin: "device",
    trust: "trusted",
    untrusted_origin: "",
    privacy_level: restricted ? "restricted" : privacy,
    fault_level: inferFault(event),
    severity: event.severity,
    component_id: "device/client",
    refs: {
      task_id: event.task_id ?? undefined,
      run_id: event.run_id ?? undefined,
    },
    payload: restricted ? {} : event.payload,
    payload_ref: restricted ? event.event_id : null,
    redacted: restricted,
  };
}
