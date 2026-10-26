import assert from "node:assert/strict";
import { test } from "node:test";
import { parseResourceSample, backgroundResourceDecision } from "../../src/device/resourceSampler.ts";

const now = 1800000000000;
const raw = { valid: true, sampledAtMs: now - 1, cpuFraction: 0.2, memoryFraction: 0.2 };
for (const cpu of [undefined, null, NaN, Infinity, -1, 2]) {
  test(`CPU不可用不冒称低压力: ${String(cpu)}`, () => {
    const parsed = parseResourceSample({ ...raw, cpuFraction: cpu }, { nowMs: now });
    assert.equal(parsed.valid, false);
    assert.equal(backgroundResourceDecision(parsed, { highWater: 0.9 }).allow, false);
  });
}
for (const nowMs of [NaN, Infinity, -Infinity, 0]) {
  test(`无效时钟不放行: ${String(nowMs)}`, () => {
    assert.equal(parseResourceSample(raw, { nowMs }).valid, false);
  });
}
test("未来时间不通过钳制年龄冒充新鲜采样", () => {
  assert.equal(parseResourceSample({ ...raw, sampledAtMs: now + 60000 }, { nowMs: now }).valid, false);
});
test("导出的判定不信任伪造valid标记与NaN压力", () => {
  const sample = parseResourceSample(raw, { nowMs: now });
  assert.equal(backgroundResourceDecision({ ...sample, cpuFraction: NaN }, { highWater: 0.9 }).allow, false);
});
