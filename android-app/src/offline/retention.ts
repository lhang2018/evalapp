/**
 * 容量治理（补充 C §4）：按事件类别分级保留。
 * 关键故障/验收证据永不自动清理；其余过期或空间紧张时清理并记录损失标记。
 */

import type { RetentionTier } from "./types.ts";

export const RETENTION_MS: Record<RetentionTier, number | null> = {
  critical: null,
  normal: 7 * 24 * 3600 * 1000,
  debug: 24 * 3600 * 1000,
};

const CRITICAL_RE =
  /(diagnos|verif|accept|fail|error|security|permission|privacy|safety)/i;
const DEBUG_RE = /(debug|trace|heartbeat|poll|metric)/i;

export function classifyRetention(
  eventType: string,
  severity: string,
): RetentionTier {
  if (severity === "error" || severity === "critical") return "critical";
  if (CRITICAL_RE.test(eventType)) return "critical";
  if (DEBUG_RE.test(eventType)) return "debug";
  return "normal";
}

export function isExpired(
  retention: RetentionTier,
  timestamp: number,
  now: number,
): boolean {
  const ttl = RETENTION_MS[retention];
  if (ttl === null || !Number.isFinite(timestamp)) return false;
  return now - timestamp > ttl;
}
