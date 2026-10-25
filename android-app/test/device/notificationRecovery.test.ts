import { test } from "node:test";
import assert from "node:assert/strict";

import { TaskNotifier } from "../../src/device/notifications.ts";
import type { NotificationMarkerStore } from "../../src/device/notifications.ts";
import type { LocalTask } from "../../src/offline/types.ts";

const MARKER = "evoagent_task_notified:";
const BASELINE = `${MARKER}baseline:v1`;

function task(overrides: Partial<LocalTask> = {}): LocalTask {
  return {
    local_task_id: "local-1",
    client_request_id: "req-1",
    server_task_id: "srv-1",
    goal: "写一封邮件",
    intent: "email",
    risk_level: "low",
    status: "SUBMITTED",
    result: null,
    failure_reason: null,
    created_at: 1,
    updated_at: 1,
    last_sync_error: null,
    ...overrides,
  };
}

class FakeModule {
  calls: Array<{ id: number; title: string; body: string }> = [];
  fail = false;
  async notifyTaskUpdate(id: number, title: string, body: string) {
    if (this.fail) throw new Error("notification failed");
    this.calls.push({ id, title, body });
    return true;
  }
}

class FakeStore implements NotificationMarkerStore {
  rows = new Map<string, string>();
  // 读取失败只针对指定前缀（"" 表示全部），便于区分"基线读失败"与"任务标记读失败"。
  failReadPrefixes: string[] = [];
  failWrite = false;
  writeCount = 0;
  async getItem(key: string) {
    if (this.failReadPrefixes.some((prefix) => key.startsWith(prefix))) {
      throw new Error("read failed");
    }
    return this.rows.get(key) ?? null;
  }
  async setItem(key: string, value: string) {
    if (this.failWrite) throw new Error("write failed");
    this.writeCount += 1;
    this.rows.set(key, value);
  }
}

test("没有标记存储时保持旧语义：同步已先行完成不补发", async () => {
  const module = new FakeModule();
  const notifier = new TaskNotifier(module);
  const done = task({ status: "DONE", server_status: "SUCCEEDED" });
  assert.equal(await notifier.notifyTransitions([done], [done]), 0);
  assert.equal(module.calls.length, 0);
});

test("同步已先行完成时用持久化标记补发一次且不重复", async () => {
  const module = new FakeModule();
  const store = new FakeStore();
  const notifier = new TaskNotifier(module, store);
  await notifier.initializeExisting([]);
  assert.equal(store.rows.get(BASELINE), "READY");
  const done = task({ status: "DONE", server_status: "SUCCEEDED" });
  assert.equal(await notifier.notifyTransitions([done], [done]), 1);
  assert.equal(module.calls.length, 1);
  assert.equal(await notifier.notifyTransitions([done], [done]), 0);
  assert.equal(module.calls.length, 1);
  assert.equal(store.rows.get(`${MARKER}local-1`), "DONE");
});

test("本次跃迁仍会投递并写入标记（不因标记路径重复）", async () => {
  const module = new FakeModule();
  const store = new FakeStore();
  const notifier = new TaskNotifier(module, store);
  const before = task({ status: "SUBMITTED" });
  const after = task({ status: "DONE", server_status: "SUCCEEDED" });
  assert.equal(await notifier.notifyTransitions([before], [after]), 1);
  assert.equal(await notifier.notifyTransitions([after], [after]), 0);
  assert.equal(module.calls.length, 1);
});

test("投递失败不写标记，下次 run 重试", async () => {
  const module = new FakeModule();
  module.fail = true;
  const store = new FakeStore();
  const notifier = new TaskNotifier(module, store);
  await notifier.initializeExisting([]);
  const done = task({ status: "DONE", server_status: "FAILED", failure_reason: "超时" });
  assert.equal(await notifier.notifyTransitions([done], [done]), 0);
  assert.equal(store.rows.has(`${MARKER}local-1`), false);
  module.fail = false;
  assert.equal(await notifier.notifyTransitions([done], [done]), 1);
  assert.equal(store.rows.get(`${MARKER}local-1`), "DONE");
});

test("标记读取失败时退化为跃迁判断且不崩溃", async () => {
  const module = new FakeModule();
  const store = new FakeStore();
  store.failReadPrefixes = [""];
  const notifier = new TaskNotifier(module, store);
  const before = task({ status: "SUBMITTED" });
  const after = task({ status: "DONE", server_status: "SUCCEEDED" });
  assert.equal(await notifier.notifyTransitions([before], [after]), 1);
  assert.equal(module.calls.length, 1);
});

test("已有持久标记会抑制陈旧快照中的重复跃迁", async () => {
  const module = new FakeModule();
  const store = new FakeStore();
  store.rows.set(`${MARKER}local-1`, "DONE");
  const notifier = new TaskNotifier(module, store);
  const before = task({ status: "SUBMITTED" });
  const after = task({ status: "DONE", server_status: "SUCCEEDED" });
  assert.equal(await notifier.notifyTransitions([before], [after]), 0);
  assert.equal(module.calls.length, 0);
});

test("首次初始化只基线化历史终态，后续新终态仍会通知", async () => {
  const module = new FakeModule();
  const store = new FakeStore();
  const notifier = new TaskNotifier(module, store);
  const historical = task({ status: "DONE", server_status: "SUCCEEDED" });
  await notifier.initializeExisting([historical]);
  assert.equal(await notifier.notifyTransitions([historical], [historical]), 0);

  const before = task({
    local_task_id: "local-2",
    status: "SUBMITTED",
  });
  const after = task({
    local_task_id: "local-2",
    status: "DONE",
    server_status: "SUCCEEDED",
  });
  assert.equal(await notifier.notifyTransitions([before], [after]), 1);
  assert.equal(module.calls.length, 1);
});

test("并发通知器共享串行门禁，不会同时穿过标记检查", async () => {
  const module = new FakeModule();
  const store = new FakeStore();
  const first = new TaskNotifier(module, store);
  const second = new TaskNotifier(module, store);
  await first.initializeExisting([]);
  const done = task({ status: "DONE", server_status: "SUCCEEDED" });
  const results = await Promise.all([
    first.notifyTransitions([done], [done]),
    second.notifyTransitions([done], [done]),
  ]);
  assert.deepEqual(results.sort(), [0, 1]);
  assert.equal(module.calls.length, 1);
});

test("无标记存储时 initializeExisting 是安全空操作", async () => {
  const notifier = new TaskNotifier(new FakeModule());
  await notifier.initializeExisting([task({ status: "DONE" })]);
  assert.equal(await notifier.notifyTransitions([], []), 0);
});

test("initializeExisting 幂等：基线就绪后不再重复写标记", async () => {
  const module = new FakeModule();
  const store = new FakeStore();
  const notifier = new TaskNotifier(module, store);
  const historical = task({ status: "DONE", server_status: "SUCCEEDED" });
  await notifier.initializeExisting([historical]);
  const writesAfterFirst = store.writeCount;
  await notifier.initializeExisting([historical]);
  assert.equal(store.writeCount, writesAfterFirst);
  assert.equal(await notifier.notifyTransitions([historical], [historical]), 0);
});

test("基线写失败不抛错且保持未就绪，恢复后补发可用", async () => {
  const module = new FakeModule();
  const store = new FakeStore();
  store.failWrite = true;
  const notifier = new TaskNotifier(module, store);
  const done = task({ status: "DONE", server_status: "SUCCEEDED" });
  await notifier.initializeExisting([done]);   // 不抛错
  assert.equal(store.rows.get(BASELINE), undefined);
  assert.equal(await notifier.notifyTransitions([done], [done]), 0);
  store.failWrite = false;
  await notifier.initializeExisting([]);
  assert.equal(store.rows.get(BASELINE), "READY");
  const fresh = task({ local_task_id: "local-9", status: "DONE",
                       server_status: "SUCCEEDED" });
  assert.equal(await notifier.notifyTransitions([fresh], [fresh]), 1);
});

test("基线读取失败时补发被禁用但不崩溃", async () => {
  const module = new FakeModule();
  const store = new FakeStore();
  store.failReadPrefixes = [BASELINE];
  const notifier = new TaskNotifier(module, store);
  const done = task({ status: "DONE", server_status: "SUCCEEDED" });
  assert.equal(await notifier.notifyTransitions([done], [done]), 0);
  assert.equal(module.calls.length, 0);
});

test("任务标记读取失败但基线就绪时仍按跃迁投递", async () => {
  const module = new FakeModule();
  const store = new FakeStore();
  await new TaskNotifier(module, store).initializeExisting([]);
  store.failReadPrefixes = [`${MARKER}local-`];
  const notifier = new TaskNotifier(module, store);
  const before = task({ status: "SUBMITTED" });
  const after = task({ status: "DONE", server_status: "SUCCEEDED" });
  assert.equal(await notifier.notifyTransitions([before], [after]), 1);
  assert.equal(module.calls.length, 1);
});

test("基线读取暂时失败不得把待补发任务误写为历史已通知", async () => {
  const module = new FakeModule();
  const store = new FakeStore();
  const notifier = new TaskNotifier(module, store);
  await notifier.initializeExisting([]);
  const done = task({ status: "DONE", server_status: "SUCCEEDED" });
  store.failReadPrefixes = [BASELINE];
  await notifier.initializeExisting([done]);
  assert.equal(store.rows.has(`${MARKER}local-1`), false);
  store.failReadPrefixes = [];
  assert.equal(await notifier.notifyTransitions([done], [done]), 1);
});

test("系统拒绝投递不写标记，下一通知器仍可补发", async () => {
  const store = new FakeStore();
  const rejected = new TaskNotifier({ notifyTaskUpdate: async () => false }, store);
  await rejected.initializeExisting([]);
  const done = task({ status: "DONE", server_status: "SUCCEEDED" });
  assert.equal(await rejected.notifyTransitions([done], [done]), 0);
  assert.equal(store.rows.has(`${MARKER}local-1`), false);
  const accepted = new TaskNotifier(new FakeModule(), store);
  assert.equal(await accepted.notifyTransitions([done], [done]), 1);
});

test("系统接受后标记写失败允许重试，不能宣称恰好一次", async () => {
  const store = new FakeStore();
  const module = new FakeModule();
  const notifier = new TaskNotifier(module, store);
  await notifier.initializeExisting([]);
  const done = task({ status: "DONE", server_status: "SUCCEEDED" });
  store.failWrite = true;
  assert.equal(await notifier.notifyTransitions([done], [done]), 1);
  assert.equal(store.rows.has(`${MARKER}local-1`), false);
  store.failWrite = false;
  assert.equal(await new TaskNotifier(module, store).notifyTransitions([done], [done]), 1);
  assert.equal(module.calls.length, 2);
  assert.equal(module.calls[0].id, module.calls[1].id);
  assert.equal(await notifier.notifyTransitions([done], [done]), 0);
});
