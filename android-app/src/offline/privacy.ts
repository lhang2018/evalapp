/** 事件隐私分级（PRD 75/86）：只有 standard 可上传。 */

import type { PrivacyLevel } from "./types.ts";

const SENSITIVE_EVENTS = new Set([
  "device_action",
  "device_action_result",
  "location_update",
  "memory_write",
  "user_private_note",
]);

const SECRET_KEY_RE =
  /(password|passwd|token|secret|api[_-]?key|authorization|credit[_-]?card|id[_-]?card|身份证|密码)/i;
const SENSITIVE_KEY_RE =
  /(phone|email|address|location|位置|电话|邮箱|住址|联系人)/i;

function matchesKeys(value: unknown, pattern: RegExp, depth = 0): boolean {
  if (depth > 5 || value === null || typeof value !== "object") return false;
  if (Array.isArray(value)) {
    return value.some((item) => matchesKeys(item, pattern, depth + 1));
  }
  return Object.entries(value as Record<string, unknown>).some(
    ([key, item]) => pattern.test(key) || matchesKeys(item, pattern, depth + 1),
  );
}

export function classifyPrivacy(
  eventType: string,
  payload: Record<string, unknown> = {},
): PrivacyLevel {
  if (eventType.startsWith("secret") || matchesKeys(payload, SECRET_KEY_RE)) {
    return "secret";
  }
  if (SENSITIVE_EVENTS.has(eventType) ||
      matchesKeys(payload, SENSITIVE_KEY_RE)) {
    return "sensitive";
  }
  return "standard";
}

export function isUploadable(level: PrivacyLevel): boolean {
  return level === "standard";
}
