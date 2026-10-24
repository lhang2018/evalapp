/** Test entry only: imports unchanged candidate modules; never part of product index.js. */
import React, { useEffect, useState } from 'react';
import { AppRegistry, NativeModules, ScrollView, Text } from 'react-native';
import AsyncStorage from '@react-native-async-storage/async-storage';
import { DeviceBroker } from './src/device/broker.ts';
import { NativeDeviceTransport } from './src/device/nativeTransport.ts';
import { TaskActionLedger, createProposal, executeProposal } from './src/device/taskProtocol.ts';
import { BundleRuntime } from './src/update/bundleRuntime.ts';
import { UpdateManager } from './src/update/manager.ts';
import { WorkflowBundleApplier, loadActiveBundle } from './src/update/workflowBundle.ts';
import { NativeArtifactFiles, FetchArtifactSource } from './src/update/nativeArtifact.ts';
import { TrustedKeyRing } from './src/update/trust.ts';
const fixture = require('./device-review-fixtures.json');
const PREFIX = 'review_f550d90_20261024_v2/';
const REPORT = PREFIX + 'report';
const PHASE = PREFIX + 'phase';
const store = (area: string) => ({
  getItem: (key: string) => AsyncStorage.getItem(PREFIX + area + '/' + key),
  setItem: (key: string, value: string) => AsyncStorage.setItem(PREFIX + area + '/' + key, value),
  removeItem: (key: string) => AsyncStorage.removeItem(PREFIX + area + '/' + key),
});
const ledgerKV = store('ledger'), updateKV = store('update'), timeoutKV = store('timeout');
function check(value: unknown, message: string): asserts value { if (!value) throw new Error(message); }
const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
const proposal = (name: string) => ({ ...createProposal({ task_id: 'device-review-task', run_id: 'device-review-run',
  kind: 'launch_intent', target: 'com.evoagent', now: 1 }), action_id: 'device-review-' + name,
  proposal_id: 'proposal-' + name, effect_id: 'effect-' + name });
let target = fixture.targets.good;
const reports: any[] = [];
const transport = {
  reconcile: async () => ({ installation_id: 'review-device', decision: 'update_available',
    update: { available: true, target_release_id: target.release_id, in_gray_scope: true, gray_percent: 100 },
    target, current_release_id: null, checks: [], reasons: [], server_time: Date.now() }),
  applyResult: async (value: any) => { reports.push(value); return { ok: true }; },
  candidateStates: async () => ({ installation_id: 'review-device', current_release_id: null, candidates: [], server_time: 1 }),
};
function manager(kv: any, area: string) {
  const files = new NativeArtifactFiles();
  return new UpdateManager(transport as any, kv, { appVersion: '0.1.0', installationId: 'review-device',
    toolContracts: [], artifactFiles: files, artifactSource: new FetchArtifactSource(),
    trustedKeys: new TrustedKeyRing([fixture.key]), bundleApplier: new WorkflowBundleApplier(files, kv),
    grantedPermissions: () => [], stagingDir: 'review_f550d90/' + area });
}
const localTools = { local_time: async (args: any) => ({ time: new Date().toISOString(), label: args.label }) };
const runtime = () => new BundleRuntime(updateKV, { appVersion: '0.1.0', tools: localTools, executionTimeoutMs: 2000 });

async function run(update: (value: string) => void) {
  const existing = await AsyncStorage.getItem(REPORT);
  const report: any = existing ? JSON.parse(existing) : { source_commit: fixture.source_commit, source_tree: fixture.source_tree,
    engine: (global as any).HermesInternal ? 'Hermes' : 'other', tests: [],
    limits: ['test entry imports actual modules; not natural-language App main-chain',
      'device dispatch counter uses an explicit transport fixture; no accessibility permission bypass',
      'reconcile/applyResult use an explicit fixture; downloads, RSA verification, native files and AsyncStorage are real'] };
  const save = async () => { await AsyncStorage.setItem(REPORT, JSON.stringify(report)); update(JSON.stringify(report, null, 2)); };
  const test = async (name: string, body: () => Promise<any>) => {
    try { const detail = await body(); report.tests.push({ name, ok: true, detail: detail ?? null }); }
    catch (e) { report.tests.push({ name, ok: false, error: String(e) }); await save(); throw e; }
    await save();
  };
  const phase = await AsyncStorage.getItem(PHASE);
  if (phase === 'done') { await save(); return; }
  if (!phase) {
    await test('native_environment_and_permission_boundary', async () => {
      check(report.engine === 'Hermes', 'must execute on device Hermes');
      check(NativeModules.EvoAgentDevice && NativeModules.EvoAgentUpdate, 'native modules missing');
      const native = new NativeDeviceTransport(NativeModules.EvoAgentDevice);
      const available = await native.isAvailable();
      if (!available) {
        const result = await new DeviceBroker(native).execute({ action_id: 'review-unavailable', kind: 'launch_intent', target: 'com.evoagent' });
        check(result.status === 'unavailable', 'permission boundary must refuse');
      }
      return { available, native_file_module: true, durable_storage: 'AsyncStorage' };
    });
    await test('concurrent_same_action_dispatches_once', async () => {
      let calls = 0;
      const broker = () => new DeviceBroker({ isAvailable: async () => true, dispatch: async () => {
        calls++; await delay(30); return { status: 'verified', evidence: { fixture: true } }; } });
      await Promise.all([executeProposal(new TaskActionLedger(ledgerKV), broker(), proposal('once')),
        executeProposal(new TaskActionLedger(ledgerKV), broker(), proposal('once'))]);
      check(calls === 1, 'duplicate side effect');
      const receipt = await new TaskActionLedger(ledgerKV).getReceipt('device-review-once');
      check(receipt?.state === 'verified', 'final receipt missing');
      return { dispatches: calls, receipt: receipt?.state };
    });
    await test('changed_identity_and_corrupt_history_refuse', async () => {
      let calls = 0, refused = false;
      const broker = new DeviceBroker({ isAvailable: async () => true, dispatch: async () => { calls++; return { status: 'verified' }; } });
      try { await executeProposal(new TaskActionLedger(ledgerKV), broker, { ...proposal('once'), task_id: 'other' }); }
      catch { refused = true; }
      check(refused && calls === 0, 'changed identity accepted');
      const bad = store('corrupt'); await bad.setItem('device_task_ledger_v1', 'bad-json'); refused = false;
      try { await executeProposal(new TaskActionLedger(bad), broker, proposal('corrupt')); } catch { refused = true; }
      check(refused && calls === 0, 'corrupt history was discarded');
    });
    await test('signed_good_bundle_download_native_install_and_health', async () => {
      target = fixture.targets.good; const m = manager(updateKV, 'update');
      await m.check(); await m.prepare(); await m.activate();
      const probe = await runtime().runHealthProbe('device-review-good');
      check(probe.outcome === 'healthy', probe.reason);
      const state = await m.healthCheck(true, probe);
      check(state.last_known_good?.digest === target.artifact_sha256, 'wrong known good');
      return { digest: state.last_known_good?.digest, probe: probe.outcome };
    });
    await test('unhealthy_bundle_rolls_back_and_executes_old_native_artifact', async () => {
      target = fixture.targets.bad; const m = manager(updateKV, 'update');
      await m.check(); await m.prepare(); await m.activate();
      const probe = await runtime().runHealthProbe('device-review-bad');
      check(probe.outcome === 'unhealthy', probe.reason);
      const state = await m.healthCheck(false, probe);
      check(state.phase === 'rolled_back', 'did not roll back');
      const recovery = await runtime().verifyRecovery(probe.reason);
      check(recovery.outcome === 'succeeded', 'old bundle recovery failed');
      check(recovery.failed?.digest === fixture.targets.bad.artifact_sha256, 'failed identity lost');
      check(recovery.recovered?.digest === fixture.targets.good.artifact_sha256, 'recovered identity wrong');
      return { phase: state.phase, recovery, receipts: reports.map(r => r.action) };
    });
    await test('hard_tool_timeout_no_retry_or_downstream_after_late_completion', async () => {
      target = fixture.targets.timeout; const m = manager(timeoutKV, 'timeout');
      await m.check(); await m.prepare(); await m.activate();
      let calls = 0, dependent = 0, complete: (v: string) => void = () => {};
      const rt = new BundleRuntime(timeoutKV, { appVersion: '0.1.0', executionTimeoutMs: 150,
        tools: { hang: async () => { calls++; return new Promise(resolve => { complete = resolve; }); },
          after: async () => { dependent++; return 'after'; } } });
      const started = Date.now(); const result = await rt.execute({}, 'device-review-timeout');
      const elapsed = Date.now() - started; check(result.outcome === 'timeout' && elapsed < 3000, 'unbounded tool');
      complete('late'); await delay(100);
      check(calls === 1 && dependent === 0, 'timeout retried or ran downstream');
      const probe = await rt.runHealthProbe('device-review-timeout');
      check(probe.outcome === 'unknown', 'timeout falsely healthy'); complete('late-probe');
      return { outcome: result.outcome, elapsed_ms: elapsed, calls_before_probe: 1, dependent, probe: probe.outcome };
    });
    await test('stale_probe_cannot_promote_current_artifact', async () => {
      const m = manager(updateKV, 'update');
      // rolled_back cannot be certified; after normal check it is active but still old.
      target = fixture.targets.good; await m.check();
      let refused = false;
      try { await m.healthCheck(true, { release_id: 'other', version: '9.0.0', digest: 'other' }); } catch { refused = true; }
      check(refused, 'stale probe promoted');
    });
    await new TaskActionLedger(ledgerKV).recordProposal(proposal('interrupted'));
    await AsyncStorage.setItem(PHASE, 'awaiting-force-stop');
    report.phase = 'awaiting-force-stop'; await save();
  } else {
    await test('actual_force_stop_restart_keeps_receipts_and_never_replays_unknown', async () => {
      let calls = 0;
      const broker = new DeviceBroker({ isAvailable: async () => true, dispatch: async () => { calls++; return { status: 'verified' }; } });
      const ledger = new TaskActionLedger(ledgerKV);
      const complete = await executeProposal(ledger, broker, proposal('once'));
      const unknown = await executeProposal(ledger, broker, proposal('interrupted'));
      check(!complete.dispatched && complete.receipt.replayed, 'completed action replayed');
      check(!unknown.dispatched && unknown.receipt.state === 'unverified' && !unknown.receipt.terminal, 'interrupted action replayed');
      check(calls === 0, 'restart dispatched side effect');
      return { dispatches: calls, completed: complete.receipt.state, interrupted: unknown.receipt.state };
    });
    await test('actual_restart_keeps_bundle_binding_and_old_capability_execution', async () => {
      const rt = runtime(), prior = await rt.loadState();
      check(prior.recovery?.recovered?.digest === fixture.targets.good.artifact_sha256, 'recovery record lost');
      const active = await loadActiveBundle(updateKV);
      check(active?.digest === fixture.targets.good.artifact_sha256, 'wrong active artifact');
      const record = await rt.execute({}, 'device-review-post-restart');
      check(record.outcome === 'succeeded' && record.digest === active?.digest, 'old capability stopped working');
      return { active: active?.release_id, digest: record.digest, outcome: record.outcome, recovery: prior.recovery?.outcome };
    });
    report.phase = 'done'; await AsyncStorage.setItem(PHASE, 'done'); await save();
  }
}

function DeviceReview() {
  const [text, setText] = useState('Candidate device module regression starting');
  useEffect(() => { run(setText).catch(e => setText(t => t + '\nFAILED: ' + String(e))); }, []);
  return <ScrollView style={{ backgroundColor: '#fff', padding: 18 }}><Text style={{ color: '#111', fontSize: 15 }}>{text}</Text></ScrollView>;
}
AppRegistry.registerComponent('EvoAgent', () => DeviceReview);
