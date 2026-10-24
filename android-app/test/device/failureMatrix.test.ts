import { test } from "node:test";
import assert from "node:assert/strict";

import {
  FAILURE_KINDS,
  FAILURE_MATRIX,
  handlingFor,
  handlingForKind,
} from "../../src/device/failureMatrix.ts";
import type { FailureHandling, FailureKind } from "../../src/device/failureMatrix.ts";

test("矩阵覆盖任务卡要求的全部故障类别", () => {
  const required: FailureKind[] = [
    "server_unreachable",   // 服务端不可达
    "model_timeout",        // 模型超时
    "model_rate_limited",   // 模型限流
    "network_switch",       // 网络切换
    "offline",              // 断网
    "sync_conflict",        // 同步冲突
    "process_killed",       // 进程被杀
    "app_restart",          // 重启
    "low_battery",          // 低电量
    "low_storage",          // 低空间
    "accessibility_unauthorized", // 无障碍服务未授权
  ];
  for (const kind of required) {
    assert.ok(FAILURE_MATRIX[kind], `矩阵缺 ${kind}`);
  }
  assert.deepEqual(new Set(FAILURE_KINDS).size, FAILURE_KINDS.length);
});

test("每个条目六要素齐备：可见状态/降级/重试/停止/对账/设备动作规则", () => {
  for (const kind of FAILURE_KINDS) {
    const entry: FailureHandling = FAILURE_MATRIX[kind];
    assert.ok(entry.user_visible_state.trim(), `${kind} 缺用户可见状态`);
    assert.ok(entry.degradation.trim(), `${kind} 缺降级动作`);
    assert.ok(entry.retry.policy, `${kind} 缺重试策略`);
    assert.ok(entry.stop_condition.trim(), `${kind} 缺停止条件`);
    assert.ok(entry.reconciliation.trim(), `${kind} 缺对账方式`);
    assert.ok(entry.device_action_rule.trim(), `${kind} 缺设备动作规则`);
    assert.ok(entry.local_retention.length > 0, `${kind} 未列明本地保留字段`);
  }
});

test("服务端不可达/断网：退避重试 + 死信不静默丢弃 + 幂等对账", () => {
  const entry = FAILURE_MATRIX.server_unreachable;
  assert.equal(entry.retry.policy, "exponential_backoff");
  assert.match(entry.retry.detail ?? "", /2s→5min/);
  assert.match(entry.stop_condition, /死信/);
  assert.match(entry.stop_condition, /不静默丢弃/);
  assert.match(entry.reconciliation, /client_request_id/);
  assert.match(entry.reconciliation, /deduplicated|confirmed/);
  assert.match(FAILURE_MATRIX.offline.reconciliation, /版本对账/);
});

test("模型故障：预算约束 + 不本地伪造结果 + 未知计费对账", () => {
  for (const kind of ["model_timeout", "model_rate_limited"] as FailureKind[]) {
    const entry = FAILURE_MATRIX[kind];
    assert.match(entry.stop_condition, /预算/);
    assert.match(entry.reconciliation, /对账|provider/);
    assert.match(entry.device_action_rule, /不抢先执行/);
  }
});

test("进程被杀/重启：持久化恢复 + NEEDS_RECONCILE + 未验证动作不重放", () => {
  const killed = FAILURE_MATRIX.process_killed;
  assert.match(killed.degradation, /持久化|stale/);
  assert.match(killed.stop_condition, /NEEDS_RECONCILE/);
  assert.match(killed.device_action_rule, /禁止自动重放|不自动重放/);
  const restart = FAILURE_MATRIX.app_restart;
  assert.match(restart.reconciliation, /release|版本关联|对账/);
  assert.match(restart.device_action_rule, /不自动重放/);
});

test("同步冲突：隔离区 + 人工处理 + 失联不当作未执行", () => {
  const entry = FAILURE_MATRIX.sync_conflict;
  assert.equal(entry.retry.policy, "manual_only");
  assert.match(entry.degradation, /隔离区/);
  assert.match(entry.device_action_rule, /失联不当作未执行|真实回执/);
});

test("资源与权限：低电量推迟、低空间 critical 永不清理、无障碍不自动重试", () => {
  assert.equal(FAILURE_MATRIX.low_battery.retry.policy, "deferred");
  assert.match(FAILURE_MATRIX.low_battery.degradation, /电量不低|推迟/);
  const storage = FAILURE_MATRIX.low_storage;
  assert.match(storage.degradation, /80%|损失标记/);
  assert.match(storage.stop_condition, /critical.*永不自动清理/);
  const a11y = FAILURE_MATRIX.accessibility_unauthorized;
  assert.equal(a11y.retry.policy, "manual_only");
  assert.match(a11y.degradation, /unavailable|系统设置/);
  assert.match(a11y.device_action_rule, /不产生任何下发|不自动重发/);
});

test("全矩阵设备动作规则一致：未知副作用永不自动重放", () => {
  for (const kind of FAILURE_KINDS) {
    const rule = FAILURE_MATRIX[kind].device_action_rule;
    const forbids = /不自动重放|禁止自动重放|不自动重发|不产生任何下发|失联不当作未执行|不抢先执行|回执以本地记录为准|等待证据|仍不自动重放/.test(rule);
    assert.ok(forbids, `${kind} 的设备动作规则未写明不自动重放类约束: ${rule}`);
  }
});

test("查询入口：已知类别返回对应条目，未知类别按服务端不可达降级", () => {
  assert.equal(handlingFor("offline"), FAILURE_MATRIX.offline);
  assert.equal(handlingForKind("offline"), FAILURE_MATRIX.offline);
  const unknown = handlingForKind("quantum_storm");
  assert.equal(unknown, FAILURE_MATRIX.server_unreachable);
});
