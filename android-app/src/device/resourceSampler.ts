/**
 * ALN-15-E：原生资源采样适配与后台准入判定。
 *
 * - 原生模块（Android `ResourceSamplerModule`）提供 CPU/内存/电量/磁盘采样，
 *   带来源、单位、采样时间与不可用状态；
 * - 本适配层只做**校验/时效/压力计算**，不复制预算或阈值权威：
 *   `highWater` 必须由调用方从既有策略传入，缺失即拒绝（不内置默认阈值）；
 * - 缺失/过期/非法采样一律 `valid=false`，**绝不伪造成低压力放行**；
 * - 判定仅用于后台周期工作；前台任务不受该 helper 影响。
 */

export interface ResourceSample {
  valid: boolean;
  reason: string;
  cpuFraction: number | null;
  memoryFraction: number | null;
  batteryFraction: number | null;
  diskFraction: number | null;
  sampledAtMs: number | null;
  ageMs: number | null;
  source: string;
  units: Record<string, string>;
  errors: string[];
}

export interface ParseOptions {
  nowMs: number;
  maxAgeMs?: number;
}

export interface ResourceDecision {
  allow: boolean;
  reason: string;
  pressure: number | null;
}

export const DEFAULT_MAX_AGE_MS = 120_000;

function fraction(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1
    ? value
    : null;
}

function strings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string");
}

function invalidSample(reason: string, source = "", errors: string[] = []): ResourceSample {
  return {
    valid: false,
    reason,
    cpuFraction: null,
    memoryFraction: null,
    batteryFraction: null,
    diskFraction: null,
    sampledAtMs: null,
    ageMs: null,
    source,
    units: {},
    errors,
  };
}

export function parseResourceSample(raw: unknown, options: ParseOptions): ResourceSample {
  if (!raw || typeof raw !== "object") {
    return invalidSample("native_unavailable");
  }
  const value = raw as Record<string, unknown>;
  const source = typeof value.source === "string" ? value.source : "";
  const errors = strings(value.errors);
  if (value.valid !== true) {
    return invalidSample("native_reported_invalid", source, errors);
  }
  const sampledAt =
    typeof value.sampledAtMs === "number" && Number.isFinite(value.sampledAtMs) &&
    value.sampledAtMs > 0
      ? value.sampledAtMs
      : null;
  if (sampledAt === null) {
    return invalidSample("missing_sampled_at", source, errors);
  }
  if (typeof options.nowMs !== "number" || !Number.isFinite(options.nowMs) || options.nowMs <= 0) {
    return invalidSample("invalid_now", source, errors);
  }
  if (sampledAt > options.nowMs) {
    return invalidSample("future_sample", source, errors);
  }
  const ageMs = options.nowMs - sampledAt;
  const maxAge = options.maxAgeMs ?? DEFAULT_MAX_AGE_MS;
  if (!Number.isFinite(maxAge) || maxAge <= 0) {
    return invalidSample("invalid_max_age_config", source, errors);
  }
  if (ageMs > maxAge) {
    return invalidSample("stale_sample", source, errors);
  }
  const memory = fraction(value.memoryFraction);
  if (memory === null) {
    return invalidSample("missing_memory", source, errors);
  }
  const cpu = fraction(value.cpuFraction);
  if (cpu === null) {
    return invalidSample("missing_cpu", source, errors);
  }
  const units =
    value.units && typeof value.units === "object"
      ? Object.fromEntries(
          Object.entries(value.units as Record<string, unknown>)
            .filter(([, unit]) => typeof unit === "string")
            .map(([key, unit]) => [key, unit as string]),
        )
      : {};
  return {
    valid: true,
    reason: "",
    cpuFraction: cpu,
    memoryFraction: memory,
    batteryFraction: fraction(value.batteryFraction),
    diskFraction: fraction(value.diskFraction),
    sampledAtMs: sampledAt,
    ageMs,
    source,
    units,
    errors,
  };
}

export function samplePressure(sample: ResourceSample): number | null {
  if (sample.valid !== true) return null;
  const cpu = fraction(sample.cpuFraction);
  const memory = fraction(sample.memoryFraction);
  if (cpu === null || memory === null) return null;
  return Math.max(cpu, memory);
}

export function backgroundResourceDecision(
  sample: ResourceSample,
  options: { highWater: number },
): ResourceDecision {
  const highWater = options?.highWater;
  if (typeof highWater !== "number" || !Number.isFinite(highWater) || highWater <= 0 || highWater > 1) {
    return { allow: false, reason: "invalid_high_water_config", pressure: null };
  }
  if (!sample.valid) {
    return { allow: false, reason: "sample_unavailable:" + sample.reason, pressure: null };
  }
  const pressure = samplePressure(sample);
  if (pressure === null) {
    return { allow: false, reason: "pressure_unknown", pressure: null };
  }
  if (pressure >= highWater) {
    return { allow: false, reason: "resource_high_pressure", pressure };
  }
  return { allow: true, reason: "", pressure };
}
