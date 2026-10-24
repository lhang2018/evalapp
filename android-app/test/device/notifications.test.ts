import { test } from "node:test";
import assert from "node:assert/strict";

import {
  MAX_NOTIFICATION_BODY_CHARS,
  TaskNotifier,
  describeTerminalTask,
  notificationIdFor,
} from "../../src/device/notifications.ts";
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

class FakeNotificationModule {
  calls: Array<{ id: number; title: string; body: string }> = [];
  fail = false;

  async notifyTaskUpdate(id: number, title: string, body: string) {
    if (this.fail) throw new Error("notification failed");
    this.calls.push({ id, title, body });
    return true;
  }
}

test("notification id is stable, positive, and task-specific", () => {
  const id = notificationIdFor("local-abc");
  assert.equal(id, notificationIdFor("local-abc"));
  assert.ok(id > 0);
  assert.ok(id <= 0x7fffffff);
  assert.notEqual(id, notificationIdFor("local-abd"));
});

test("non-terminal tasks produce no notification", () => {
  assert.equal(describeTerminalTask(task()), null);
  assert.equal(
    describeTerminalTask(task({ status: "LOCAL_ONLY" })),
    null,
  );
});

test("terminal task uses result, failure reason, or goal as body", () => {
  const success = describeTerminalTask(
    task({ status: "DONE", result: "邮件已发送" }),
  );
  assert.equal(success?.title, "任务完成");
  assert.equal(success?.body, "邮件已发送");

  const failed = describeTerminalTask(
    task({ status: "DONE", failure_reason: "工具超时" }),
  );
  assert.equal(failed?.title, "任务失败");
  assert.equal(failed?.body, "工具超时");

  const fallback = describeTerminalTask(task({ status: "DONE" }));
  assert.equal(fallback?.body, "写一封邮件");

  const emptyGoal = describeTerminalTask(
    task({ status: "DONE", goal: "   " }),
  );
  assert.equal(emptyGoal?.body, "任务已完成");
});

test("notification body collapses whitespace and truncates", () => {
  const result = describeTerminalTask(
    task({ status: "DONE", result: `  第一行\n\n${"长".repeat(400)}  ` }),
  );
  assert.ok(result);
  assert.ok(!result.body.includes("\n"));
  assert.ok(result.body.length <= MAX_NOTIFICATION_BODY_CHARS);
  assert.ok(result.body.endsWith("…"));
});

test("only transitions into DONE are selected", () => {
  const notifier = new TaskNotifier(null);
  const before = [
    task({ local_task_id: "a", status: "SUBMITTED" }),
    task({ local_task_id: "b", status: "DONE" }),
  ];
  const after = [
    task({ local_task_id: "a", status: "DONE", result: "完成" }),
    task({ local_task_id: "b", status: "DONE", result: "完成" }),
    task({ local_task_id: "c", status: "DONE", result: "新任务" }),
  ];
  const completed = notifier.completed(before, after).map((t) => t.local_task_id);
  assert.deepEqual(completed, ["a"]);
});

test("transitions are delivered once each with the stable id", async () => {
  const module = new FakeNotificationModule();
  const notifier = new TaskNotifier(module);
  const before = [task({ local_task_id: "a", status: "SUBMITTED" })];
  const after = [
    task({ local_task_id: "a", status: "DONE", result: "完成" }),
  ];
  const delivered = await notifier.notifyTransitions(before, after);
  assert.equal(delivered, 1);
  assert.equal(module.calls.length, 1);
  assert.equal(module.calls[0].title, "任务完成");
  assert.equal(module.calls[0].body, "完成");
  assert.equal(module.calls[0].id, notificationIdFor("a"));
});

test("missing native module degrades to a no-op", async () => {
  const notifier = new TaskNotifier(null);
  const before = [task({ status: "SUBMITTED" })];
  const after = [task({ status: "DONE", result: "完成" })];
  assert.equal(await notifier.notifyTransitions(before, after), 0);
});

test("notification failure never breaks the sync path", async () => {
  const module = new FakeNotificationModule();
  module.fail = true;
  const notifier = new TaskNotifier(module);
  const before = [
    task({ local_task_id: "a", status: "SUBMITTED" }),
    task({ local_task_id: "b", status: "SUBMITTED" }),
  ];
  const after = [
    task({ local_task_id: "a", status: "DONE", result: "完成" }),
    task({ local_task_id: "b", status: "DONE", result: "完成" }),
  ];
  assert.equal(await notifier.notifyTransitions(before, after), 0);
});
