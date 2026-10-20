import { test } from "node:test";
import assert from "node:assert/strict";

import { NativeBackgroundSync } from "../../src/offline/backgroundNative.ts";
import type { BackgroundSyncModuleLike } from "../../src/offline/backgroundNative.ts";

class FakeBackgroundModule implements BackgroundSyncModuleLike {
  intervalCalls: number[] = [];
  runNowFlags: boolean[] = [];
  statusCalls = 0;
  cancelCalls = 0;
  nowCalls = 0;

  async backgroundSyncStatus(): Promise<unknown> {
    this.statusCalls += 1;
    return { enabled: true, interval_minutes: 45, last_attempt_at: 1_700_000_000_000 };
  }

  async scheduleBackgroundSync(
    intervalMinutes: number,
    runNow: boolean,
  ): Promise<unknown> {
    this.intervalCalls.push(intervalMinutes);
    this.runNowFlags.push(runNow);
    return {
      enabled: true,
      interval_minutes: intervalMinutes,
      last_attempt_at: 0,
    };
  }

  async cancelBackgroundSync(): Promise<unknown> {
    this.cancelCalls += 1;
    return { enabled: false, interval_minutes: 45, last_attempt_at: 0 };
  }

  async runBackgroundSyncNow(): Promise<unknown> {
    this.nowCalls += 1;
    return { enabled: true, interval_minutes: 45, last_attempt_at: 0 };
  }
}

test("missing native module degrades to unavailable", async () => {
  const service = new NativeBackgroundSync(null);
  const status = await service.status();
  assert.equal(status.available, false);
  assert.equal(status.enabled, false);
  assert.equal(status.last_attempt_at, null);
  await assert.rejects(service.enable(30), /未提供后台同步原生模块/);
  await assert.rejects(service.disable(), /未提供后台同步原生模块/);
  await assert.rejects(service.runNow(), /未提供后台同步原生模块/);
});

test("status maps native values and treats zero as no attempt", async () => {
  const module = new FakeBackgroundModule();
  const service = new NativeBackgroundSync(module);
  const status = await service.status();
  assert.equal(status.available, true);
  assert.equal(status.enabled, true);
  assert.equal(status.interval_minutes, 45);
  assert.equal(status.last_attempt_at, 1_700_000_000_000);

  const scheduled = await service.enable(30, true);
  assert.equal(scheduled.last_attempt_at, null);
  assert.deepEqual(module.intervalCalls, [30]);
  assert.deepEqual(module.runNowFlags, [true]);
});

test("enable clamps invalid intervals before calling native", async () => {
  const module = new FakeBackgroundModule();
  const service = new NativeBackgroundSync(module);
  await service.enable(1);
  await service.enable(10_000);
  assert.deepEqual(module.intervalCalls, [15, 1440]);
});

test("disable and run-now delegate once", async () => {
  const module = new FakeBackgroundModule();
  const service = new NativeBackgroundSync(module);
  const disabled = await service.disable();
  assert.equal(disabled.enabled, false);
  await service.runNow();
  assert.equal(module.cancelCalls, 1);
  assert.equal(module.nowCalls, 1);
});
