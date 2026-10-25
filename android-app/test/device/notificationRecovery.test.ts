import { test } from "node:test";
import assert from "node:assert/strict";

import { TaskNotifier } from "../../src/device/notifications.ts";
import type { NotificationMarkerStore } from "../../src/device/notifications.ts";
import type { LocalTask } from "../../src/offline/types.ts";

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
  failRead = false;
  async getItem(key: string) {
    if (this.failRead) throw new Error("read failed");
    return this.rows.get(key) ?? null;
  }
  async setItem(key: string, value: string) {
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
  const done = task({ status: "DONE", server_status: "SUCCEEDED" });
  assert.equal(await notifier.notifyTransitions([done], [done]), 1);
  assert.equal(module.calls.length, 1);
  assert.equal(await notifier.notifyTransitions([done], [done]), 0);
  assert.equal(module.calls.length, 1);
  assert.equal(store.rows.get("evoagent_task_notified:local-1"), "DONE");
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
  const done = task({ status: "DONE", server_status: "FAILED", failure_reason: "超时" });
  assert.equal(await notifier.notifyTransitions([done], [done]), 0);
  assert.equal(store.rows.size, 0);
  module.fail = false;
  assert.equal(await notifier.notifyTransitions([done], [done]), 1);
  assert.equal(store.rows.size, 1);
});

test("标记读取失败时退化为跃迁判断且不崩溃", async () => {
  const module = new FakeModule();
  const store = new FakeStore();
  store.failRead = true;
  const notifier = new TaskNotifier(module, store);
  const before = task({ status: "SUBMITTED" });
  const after = task({ status: "DONE", server_status: "SUCCEEDED" });
  assert.equal(await notifier.notifyTransitions([before], [after]), 1);
  assert.equal(module.calls.length, 1);
});
