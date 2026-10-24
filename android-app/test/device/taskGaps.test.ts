/**
 * ALN-12-A 缺口证明夹具：在实现前运行，证明任务链路与设备动作之间
 * 的三个现状缺口。实现后本文件作为回归保留（断言转为验证契约存在）。
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { DeviceBroker } from "../../src/device/broker.ts";
import type { DeviceTransport } from "../../src/device/transport.ts";
import type { DeviceAction } from "../../src/device/types.ts";

class FakeTransport implements DeviceTransport {
  calls = 0;
  async isAvailable() {
    return true;
  }
  async dispatch(_action: DeviceAction) {
    this.calls += 1;
    return { status: "verified" as const, evidence: { node_text: "设置" } };
  }
}

test("缺口1：DeviceAction/ActionJournalEntry 无 task_id/run_id/effect_id 关联", async () => {
  const broker = new DeviceBroker(new FakeTransport());
  const action: DeviceAction = {
    action_id: "act-gap-1",
    kind: "launch_intent",
    target: "com.android.settings",
  };
  await broker.execute(action);
  const entry = broker.getJournal("act-gap-1");
  assert.ok(entry);
  // 现状：日志条目没有任何任务链路字段——任务无法追溯到效果
  assert.equal((entry as unknown as Record<string, unknown>).task_id, undefined);
  assert.equal((entry as unknown as Record<string, unknown>).run_id, undefined);
  assert.equal((entry as unknown as Record<string, unknown>).effect_id, undefined);
});

test("缺口2：broker 幂等日志不持久化——重启后同 action_id 会重复下发", async () => {
  let calls = 0;
  const transport: DeviceTransport = {
    async isAvailable() {
      return true;
    },
    async dispatch(_action: DeviceAction) {
      calls += 1;
      return { status: "verified" as const, evidence: { ok: true } };
    },
  };
  const first = new DeviceBroker(transport);
  await first.execute({
    action_id: "act-gap-2", kind: "launch_intent",
    target: "com.android.settings",
  });
  assert.equal(calls, 1);
  // 模拟进程重启：新 broker 实例（内存日志为空）
  const restarted = new DeviceBroker(transport);
  await restarted.execute({
    action_id: "act-gap-2", kind: "launch_intent",
    target: "com.android.settings",
  });
  // 现状：重启后重复下发——设备副作用可能发生两次
  assert.equal(calls, 2);
});

test("缺口3：无统一 effect 引用与回执投影——任务侧拿不到可上传的回执", async () => {
  const broker = new DeviceBroker(new FakeTransport());
  const result = await broker.execute({
    action_id: "act-gap-3", kind: "launch_intent",
    target: "com.android.settings",
  });
  assert.equal(result.status, "verified");
  // 现状：除了内存 journal，没有任何「任务 → 效果 → 可上传回执」结构；
  // evidence 含界面文本快照，也没有本地/上传字段切分。
  assert.ok(result.evidence);
  const protocol = (globalThis as unknown as Record<string, unknown>).DeviceTaskProtocol;
  assert.equal(protocol, undefined);
});
