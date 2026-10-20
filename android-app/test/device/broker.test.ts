import { test } from "node:test";
import assert from "node:assert/strict";

import { DeviceBroker } from "../../src/device/broker.ts";
import { NativeDeviceTransport } from "../../src/device/nativeTransport.ts";
import type { NativeDeviceModuleLike } from "../../src/device/nativeTransport.ts";
import type { DeviceTransport } from "../../src/device/transport.ts";
import type { DeviceAction, DeviceResult } from "../../src/device/types.ts";

class FakeTransport implements DeviceTransport {
  calls = 0;
  available = true;
  result: DeviceResult = {
    status: "verified",
    evidence: { node_text: "设置" },
  };

  async isAvailable() {
    return this.available;
  }

  async dispatch(_action: DeviceAction) {
    this.calls += 1;
    return this.result;
  }
}

const action = (overrides: Partial<DeviceAction> = {}): DeviceAction => ({
  action_id: "act-0001",
  kind: "launch_intent",
  target: "com.android.settings",
  ...overrides,
});

test("high-risk actions require independent confirmation", async () => {
  const transport = new FakeTransport();
  const broker = new DeviceBroker(transport);

  const result = await broker.execute(
    action({ kind: "send_message", target: "张三" }),
  );
  assert.equal(result.status, "denied");
  assert.equal(transport.calls, 0);
  assert.equal(broker.getJournal("act-0001")?.state, "denied");
});

test("confirmed high-risk action executes and records evidence", async () => {
  const transport = new FakeTransport();
  const confirmations: string[] = [];
  const broker = new DeviceBroker(transport, {
    confirm: (candidate, risk) => {
      confirmations.push(`${candidate.kind}:${risk}`);
      return true;
    },
  });

  const result = await broker.execute(
    action({ kind: "send_message", target: "李四" }),
  );
  assert.equal(result.status, "verified");
  assert.deepEqual(confirmations, ["send_message:high"]);
  assert.equal(transport.calls, 1);
  assert.deepEqual(broker.getJournal("act-0001")?.evidence, {
    node_text: "设置",
  });
});

test("transport errors become failed rather than throwing", async () => {
  const transport = new FakeTransport();
  transport.dispatch = async () => {
    throw new Error("service crashed");
  };
  const broker = new DeviceBroker(transport);
  const result = await broker.execute(action());
  assert.equal(result.status, "failed");
  assert.match(result.message ?? "", /service crashed/);
  assert.equal(broker.getJournal("act-0001")?.state, "failed");
});

test("verified results without evidence are downgraded to unverified", async () => {
  const transport = new FakeTransport();
  transport.result = { status: "verified" };
  const broker = new DeviceBroker(transport);
  const result = await broker.execute(action());
  assert.equal(result.status, "unverified");
  assert.equal(broker.getJournal("act-0001")?.state, "unverified");
});

test("repeating an action_id never replays the device action", async () => {
  const transport = new FakeTransport();
  const broker = new DeviceBroker(transport);

  const first = await broker.execute(action());
  const second = await broker.execute(action());
  assert.equal(first.status, "verified");
  assert.equal(second.status, "verified");
  assert.equal(transport.calls, 1);
  assert.match(second.message ?? "", /幂等/);

  const other = await broker.execute(action({ action_id: "act-0002" }));
  assert.equal(other.status, "verified");
  assert.equal(transport.calls, 2);
});

test("dispatched-but-unverified actions are not replayed", async () => {
  const transport = new FakeTransport();
  transport.result = { status: "unverified", message: "节点未找到" };
  const broker = new DeviceBroker(transport);

  const first = await broker.execute(action({ kind: "click_text", target: "确定" }));
  const again = await broker.execute(action({ kind: "click_text", target: "确定" }));
  assert.equal(first.status, "unverified");
  assert.equal(again.status, "unverified");
  assert.match(again.message ?? "", /禁止自动重放/);
  assert.equal(transport.calls, 1);
});

test("missing native module reports unavailable without crashing", async () => {
  const broker = new DeviceBroker(new NativeDeviceTransport(null));
  const result = await broker.execute(action());
  assert.equal(result.status, "unavailable");
  assert.equal(broker.getJournal("act-0001")?.state, "unavailable");
});

const snap = (texts: string[], packageName = "com.example.app") => ({
  package_name: packageName,
  node_count: texts.length,
  truncated: false,
  nodes: texts.map((text) => ({ text })),
});

test("click results are verified only when the UI actually changed", async () => {
  const changed = new FakeTransport();
  changed.result = {
    status: "verified",
    evidence: {
      clicked_text: "保存",
      before: snap(["设置"]),
      after: snap(["设置", "已保存"]),
    },
  };
  const broker = new DeviceBroker(changed);
  const result = await broker.execute(
    action({ kind: "click_text", target: "保存" }),
  );
  assert.equal(result.status, "verified");
  const evidence = result.evidence as Record<string, unknown>;
  assert.equal((evidence.page_change as Record<string, unknown>).changed, true);

  const unchanged = new FakeTransport();
  unchanged.result = {
    status: "verified",
    evidence: {
      clicked_text: "保存",
      before: snap(["设置"]),
      after: snap(["设置"]),
    },
  };
  const strict = new DeviceBroker(unchanged);
  const second = await strict.execute(
    action({ action_id: "act-0002", kind: "click_text", target: "保存" }),
  );
  assert.equal(second.status, "unverified");
  assert.match(second.message ?? "", /未发生可观察变化/);
});

test("permission dialogs downgrade otherwise successful clicks", async () => {
  const transport = new FakeTransport();
  transport.result = {
    status: "verified",
    evidence: {
      before: snap(["设置"]),
      after: snap(["允许访问相机", "允许", "拒绝"], "com.android.permissioncontroller"),
    },
  };
  const broker = new DeviceBroker(transport);
  const result = await broker.execute(
    action({ kind: "click_text", target: "相机" }),
  );
  assert.equal(result.status, "unverified");
  assert.match(result.message ?? "", /权限弹窗/);
});

test("consecutive unverified actions trigger a safety stop until resumed", async () => {
  const transport = new FakeTransport();
  transport.result = { status: "unverified", message: "界面未变化" };
  const broker = new DeviceBroker(transport, { maxConsecutiveUnverified: 3 });

  for (let index = 0; index < 3; index += 1) {
    const result = await broker.execute(
      action({ action_id: `act-${index}`, kind: "click_text", target: "确定" }),
    );
    assert.equal(result.status, "unverified");
  }
  assert.equal(broker.isStopped(), true);

  const blocked = await broker.execute(
    action({ action_id: "act-blocked", kind: "click_text", target: "确定" }),
  );
  assert.equal(blocked.status, "denied");
  assert.match(blocked.message ?? "", /安全停止/);
  assert.equal(transport.calls, 3);

  broker.resume();
  assert.equal(broker.isStopped(), false);
  const retried = await broker.execute(
    action({ action_id: "act-resumed", kind: "click_text", target: "确定" }),
  );
  assert.equal(retried.status, "unverified");
  assert.equal(transport.calls, 4);
});

test("verified actions reset the consecutive failure counter", async () => {
  const transport = new FakeTransport();
  const broker = new DeviceBroker(transport, { maxConsecutiveUnverified: 2 });

  transport.result = { status: "unverified", message: "未变化" };
  await broker.execute(action({ action_id: "a1", kind: "click_text" }));
  transport.result = { status: "verified", evidence: { node_text: "ok" } };
  await broker.execute(action({ action_id: "a2" }));
  transport.result = { status: "unverified", message: "未变化" };
  await broker.execute(action({ action_id: "a3", kind: "click_text" }));
  assert.equal(broker.isStopped(), false);
});

test("native transport passes through evidence and failure messages", async () => {
  const module: NativeDeviceModuleLike = {
    isServiceEnabled: async () => true,
    launchIntent: async (target) => ({
      ok: true,
      evidence: { launched: target },
    }),
    performClick: async () => ({ ok: false, message: "未找到节点" }),
  };
  const transport = new NativeDeviceTransport(module);

  const launched = await transport.dispatch(
    action({ kind: "launch_intent", target: "com.example" }),
  );
  assert.equal(launched.status, "verified");
  assert.deepEqual(launched.evidence, { launched: "com.example" });

  const clicked = await transport.dispatch(
    action({ action_id: "act-0002", kind: "click_text", target: "确定" }),
  );
  assert.equal(clicked.status, "failed");
  assert.match(clicked.message ?? "", /未找到节点/);
});
