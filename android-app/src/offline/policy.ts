/** 离线可执行范围（PRD 72）：仅无外部依赖的本地能力。 */

export interface OfflineRunResult {
  result: string;
  completed_at: number;
}

export const OFFLINE_INTENTS = ["time_query"] as const;

export function canRunOffline(intent: string): boolean {
  return (OFFLINE_INTENTS as readonly string[]).includes(intent);
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

export function formatLocalTime(now: Date): string {
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(
    now.getDate(),
  )} ${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
}

export function runOffline(
  goal: string,
  intent: string,
  now: Date = new Date(),
): OfflineRunResult | null {
  if (!canRunOffline(intent)) return null;
  return {
    result: `本地时间: ${formatLocalTime(now)}`,
    completed_at: now.getTime(),
  };
}
