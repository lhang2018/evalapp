import { test } from 'node:test';
import assert from 'node:assert/strict';
import { TaskNotifier, describeTerminalTask } from '../../src/device/notifications.ts';
import type { LocalTask } from '../../src/offline/types.ts';

const task = (overrides: Partial<LocalTask> = {}): LocalTask => ({
  local_task_id: 'review-task', client_request_id: 'review-request', goal: 'private task goal',
  intent: 'general', risk_level: 'low', status: 'SUBMITTED', result: null,
  failure_reason: null, created_at: 1, updated_at: 1, ...overrides,
});
const before = () => [task()];
const after = () => [task({ status: 'DONE', server_status: 'SUCCEEDED', result: 'done' })];

for (const response of [false, undefined, null, 'true', { ok: true }]) {
  test(`native ${JSON.stringify(response)} is not an accepted notification post`, async () => {
    const notifier = new TaskNotifier({ notifyTaskUpdate: async () => response } as any);
    assert.equal(await notifier.notifyTransitions(before(), after()), 0);
  });
}

test('accepted and refused native posts are counted separately', async () => {
  let calls = 0;
  const notifier = new TaskNotifier({ notifyTaskUpdate: async () => ++calls === 2 });
  const pending = [task({ local_task_id: 'one' }), task({ local_task_id: 'two' })];
  const completed = pending.map(t => ({ ...t, status: 'DONE' as const }));
  assert.equal(await notifier.notifyTransitions(pending, completed), 1);
  assert.equal(calls, 2);
});

test('FAILED without failure text cannot be reported as success', () => {
  const notice = describeTerminalTask(task({ status: 'DONE', server_status: 'FAILED', result: 'stale success text' }));
  assert.equal(notice?.title, '任务失败');
  assert.equal(notice?.body, '任务执行失败');
});

test('CANCELLED with or without a reason is a cancellation notification', () => {
  for (const reason of [null, 'user cancelled']) {
    const notice = describeTerminalTask(task({ status: 'DONE', server_status: 'CANCELLED', failure_reason: reason }));
    assert.equal(notice?.title, '任务已取消');
    assert.equal(notice?.body, reason ?? '任务已取消');
  }
});

test('SUCCEEDED is not overridden by an old failure reason', () => {
  const notice = describeTerminalTask(task({ status: 'DONE', server_status: 'SUCCEEDED', failure_reason: 'old error', result: 'done' }));
  assert.equal(notice?.title, '任务完成');
  assert.equal(notice?.body, 'done');
});

test('nonterminal or unknown server status cannot produce a terminal notification', async () => {
  for (const status of ['RUNNING', 'SUSPENDED', 'UNKNOWN']) {
    const done = task({ status: 'DONE', server_status: status });
    assert.equal(describeTerminalTask(done), null);
    const notifier = new TaskNotifier({ notifyTaskUpdate: async () => { throw new Error('must not post'); } });
    assert.equal(await notifier.notifyTransitions(before(), [done]), 0);
  }
});
