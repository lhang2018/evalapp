import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import { emptyState, STATE_KEY } from '../../src/offline/storage.ts';
import { enqueueInto } from '../../src/offline/queue.ts';

// Exercise the real RN assembly, repository, sync engine and task refresh.
// Only the native bridge/storage port and HTTP boundary are explicit fixtures.
const state = {
  data: new Map<string, string>(), mode: '', refreshed: false, firstRead: true,
  healthCalls: 0, taskCalls: 0, nativeLookups: 0, posted: 0, syncCalls: 0,
};
const storage = {
  async getItem(key: string) {
    if (key === STATE_KEY && state.firstRead) {
      state.firstRead = false;
      if (state.mode === 'before-read') throw new Error('notification before-read failure');
    }
    if (key === STATE_KEY && state.refreshed && state.mode === 'after-read') {
      throw new Error('notification after-read failure');
    }
    return state.data.get(key) ?? null;
  },
  async setItem(key: string, value: string) {
    state.data.set(key, value);
    if (key === STATE_KEY && JSON.parse(value).tasks.some((t: any) => t.status === 'DONE')) state.refreshed = true;
  },
  async removeItem(key: string) { state.data.delete(key); },
};
const modules = Object.defineProperty({}, 'EvoAgentNotifications', { get() {
  state.nativeLookups++;
  if (state.mode === 'native-lookup') throw new Error('notification bridge failure');
  return { notifyTaskUpdate: async () => { state.posted++; return true; } };
} });
(globalThis as any).__notificationAssemblyFixture = { storage, modules };
const hooks = registerHooks({ resolve(specifier, context, next) {
  const code = specifier === 'react-native'
    ? 'export const NativeModules = globalThis.__notificationAssemblyFixture.modules;'
    : specifier === '@react-native-async-storage/async-storage'
      ? 'export default globalThis.__notificationAssemblyFixture.storage;' : null;
  return code ? { url: 'data:text/javascript,' + encodeURIComponent(code), shortCircuit: true } : next(specifier, context);
} });
const { runStoredBackgroundSync } = await import('../../src/offline/backgroundRunner.ts');
hooks.deregister();
const originalFetch = globalThis.fetch;
globalThis.fetch = async (input) => {
  const path = new URL(String(input)).pathname;
  if (path === '/health') {
    state.healthCalls++;
    if (state.mode === 'offline') throw new Error('offline');
    return new Response(JSON.stringify({ status: 'ok', provider: 'fixture' }));
  }
  if (path === '/tasks/server-review') {
    state.taskCalls++;
    return new Response(JSON.stringify({ task_status: 'SUCCEEDED', result: 'done' }));
  }
  if (path === '/events/sync') {
    state.syncCalls++;
    throw new Error('sync upload failed');
  }
  throw new Error('unexpected HTTP request: ' + path);
};
after(() => { globalThis.fetch = originalFetch; delete (globalThis as any).__notificationAssemblyFixture; });

function reset(mode: string) {
  Object.assign(state, { mode, refreshed: false, firstRead: true, healthCalls: 0, taskCalls: 0, nativeLookups: 0, posted: 0, syncCalls: 0 });
  state.data.clear();
  const initial = emptyState();
  initial.tasks = [{ local_task_id: 'review-local', client_request_id: 'review-request', server_task_id: 'server-review',
    goal: 'fixture', intent: 'general', risk_level: 'low', status: 'SUBMITTED', result: null, failure_reason: null,
    created_at: 1, updated_at: 1 }];
  if (mode === 'flush-failure') enqueueInto(initial, { event_type: 'review_event', payload: {} }, Date.now());
  state.data.set(STATE_KEY, JSON.stringify(initial));
}

for (const mode of ['native-lookup', 'after-read', 'before-read']) {
  test(`${mode} notification failure preserves actual successful sync and refresh`, async () => {
    reset(mode);
    const result = await runStoredBackgroundSync();
    assert.equal(result.ran, true);
    assert.equal(result.online, true);
    assert.equal(result.refreshed, true);
    assert.equal(result.error, null);
    assert.equal(state.healthCalls, 1);
    assert.equal(state.taskCalls, 1);
    assert.equal(JSON.parse(state.data.get(STATE_KEY)!).tasks[0].server_status, 'SUCCEEDED');
  });
}

test('offline sync retains its error and does not invoke the notification bridge', async () => {
  reset('offline');
  const result = await runStoredBackgroundSync();
  assert.equal(result.ran, false);
  assert.equal(result.online, false);
  assert.equal(result.error, 'offline');
  assert.equal(state.nativeLookups, 0);
  assert.equal(state.taskCalls, 0);
});

test('actual background assembly still posts a successful transition', async () => {
  reset('success');
  const result = await runStoredBackgroundSync();
  assert.equal(result.error, null);
  assert.equal(result.refreshed, true);
  assert.equal(state.posted, 1);
});

test('a resolved unsuccessful SyncResult is preserved as a flush failure, without notification', async () => {
  reset('flush-failure');
  const result = await runStoredBackgroundSync();
  assert.equal(result.ran, true);
  assert.equal(result.online, true);
  assert.equal(result.refreshed, false);
  assert.equal(result.error, 'sync upload failed');
  assert.equal(state.syncCalls, 1);
  assert.equal(state.taskCalls, 0);
  assert.equal(state.nativeLookups, 0);
  assert.equal(state.posted, 0);
});
