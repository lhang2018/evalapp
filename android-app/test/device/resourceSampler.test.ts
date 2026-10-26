import assert from "node:assert/strict";
import { test } from "node:test";

import {
  backgroundResourceDecision,
  parseResourceSample,
  samplePressure,
} from "../../src/device/resourceSampler.ts";

const NOW = 1_800_000_000_000;

function sample(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    valid: true,
    source: "native-fixture",
    sampledAtMs: NOW - 1_000,
    memoryFraction: 0.4,
    cpuFraction: 0.2,
    batteryFraction: 0.8,
    diskFraction: 0.5,
    errors: [],
    units: { cpuFraction: "fraction", memoryFraction: "fraction" },
    ...overrides,
  };
}

test("合法采样保留来源/单位/时间与各项分数", () => {
  const parsed = parseResourceSample(sample(), { nowMs: NOW });
  assert.equal(parsed.valid, true);
  assert.equal(parsed.source, "native-fixture");
  assert.equal(parsed.sampledAtMs, NOW - 1_000);
  assert.equal(parsed.ageMs, 1_000);
  assert.equal(parsed.cpuFraction, 0.2);
  assert.equal(parsed.memoryFraction, 0.4);
  assert.equal(parsed.units.memoryFraction, "fraction");
  assert.deepEqual(parsed.errors, []);
});

test("原生不可用/缺失内存/缺少采样时间均判不可用，不伪造低压力", () => {
  assert.equal(parseResourceSample(null, { nowMs: NOW }).reason, "native_unavailable");
  assert.equal(
    parseResourceSample(sample({ valid: false }), { nowMs: NOW }).reason,
    "native_reported_invalid",
  );
  assert.equal(
    parseResourceSample(sample({ memoryFraction: undefined }), { nowMs: NOW }).reason,
    "missing_memory",
  );
  assert.equal(
    parseResourceSample(sample({ sampledAtMs: undefined }), { nowMs: NOW }).reason,
    "missing_sampled_at",
  );
  const invalid = parseResourceSample(sample({ valid: false }), { nowMs: NOW });
  assert.equal(samplePressure(invalid), null);
  assert.equal(invalid.cpuFraction, null);
});

test("过期采样判 stale，不按旧值放行", () => {
  const parsed = parseResourceSample(
    sample({ sampledAtMs: NOW - 10 * 60_000 }),
    { nowMs: NOW, maxAgeMs: 120_000 },
  );
  assert.equal(parsed.valid, false);
  assert.equal(parsed.reason, "stale_sample");
  assert.equal(backgroundResourceDecision(parsed, { highWater: 0.9 }).allow, false);
});

test("非法分数/非法 maxAge 判不可用", () => {
  assert.equal(
    parseResourceSample(sample({ memoryFraction: 1.4 }), { nowMs: NOW }).valid,
    false,
  );
  assert.equal(
    parseResourceSample(sample({ memoryFraction: Number.NaN }), { nowMs: NOW }).valid,
    false,
  );
  assert.equal(
    parseResourceSample(sample(), { nowMs: NOW, maxAgeMs: 0 }).reason,
    "invalid_max_age_config",
  );
});

test("缺失 CPU 的部分采样不作为后台低压力依据", () => {
  const onlyMemory = parseResourceSample(
    sample({ cpuFraction: undefined, memoryFraction: 0.66 }),
    { nowMs: NOW },
  );
  assert.equal(onlyMemory.valid, false);
  assert.equal(samplePressure(onlyMemory), null);
});

test("调用方必须提供 highWater；缺失/非法即拒绝（不内置阈值权威）", () => {
  const parsed = parseResourceSample(sample(), { nowMs: NOW });
  assert.equal(
    backgroundResourceDecision(parsed, { highWater: Number.NaN }).reason,
    "invalid_high_water_config",
  );
  assert.equal(
    backgroundResourceDecision(parsed, { highWater: 0 }).allow,
    false,
  );
});

test("高压拒绝后台、低压放行，判定不影响前台", () => {
  const low = parseResourceSample(sample({ cpuFraction: 0.3 }), { nowMs: NOW });
  assert.equal(backgroundResourceDecision(low, { highWater: 0.9 }).allow, true);
  const high = parseResourceSample(sample({ memoryFraction: 0.95 }), { nowMs: NOW });
  const decision = backgroundResourceDecision(high, { highWater: 0.9 });
  assert.equal(decision.allow, false);
  assert.equal(decision.reason, "resource_high_pressure");
  assert.equal(decision.pressure, 0.95);
});
