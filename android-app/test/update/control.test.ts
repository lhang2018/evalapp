import { test } from "node:test";
import assert from "node:assert/strict";

import {
  canActivateNow,
  updateControlView,
} from "../../src/update/control.ts";
import type { UpdateState } from "../../src/update/types.ts";

function state(overrides: Partial<UpdateState> = {}): UpdateState {
  return {
    version: 1,
    installation_id: "inst_1",
    active_slot: "a",
    slots: {
      a: { release_id: "r1", version: "1.0.0", digest: "aa",
           activated_at: 1 },
      b: null,
    },
    last_known_good: null,
    pending: null,
    artifact: null,
    receipts: [],
    phase: "idle",
    target: null,
    checks: [],
    reasons: [],
    candidates: [],
    candidates_synced_at: null,
    last_checked_at: null,
    last_error: null,
    ...overrides,
  };
}

test("稳定/实验版本角色：已激活=stable，target.channel 判候选", () => {
  const v = updateControlView(state({
    phase: "active",
    target: { channel: "experimental" } as UpdateState["target"],
  }));
  assert.equal(v.currentRole, "stable");
  assert.equal(v.currentVersion, "1.0.0");
  assert.equal(v.candidateRole, "candidate");
});

test("GLOBAL-01：运行中任务禁止热切换，理由用户可读", () => {
  const st = state({ phase: "verified",
    pending: { release_id: "r2", version: "2.0.0", digest: "bb",
               activated_at: 0 } });
  const blocked = canActivateNow(st, 2);
  assert.equal(blocked.allowed, false);
  assert.ok(
    blocked.reasons.some((r) => r.includes("热切换")),
    `reasons=${JSON.stringify(blocked.reasons)}`,
  );
  const free = canActivateNow(st, 0);
  assert.equal(free.allowed, true);
});

test("无候选时激活被拒（理由可见）", () => {
  const blocked = canActivateNow(state({ phase: "idle" }), 0);
  assert.equal(blocked.allowed, false);
  assert.ok(blocked.reasons[0].includes("没有已验证"));
});

test("拒绝可见：failed 的 last_error 与 blocked 的 reasons 映射", () => {
  const failed = updateControlView(state({
    phase: "failed", last_error: "制品验签失败: 摘要不符",
  }));
  assert.match(failed.rejectionNotice ?? "", /制品验签失败/);
  const blocked = updateControlView(state({
    phase: "blocked", reasons: ["缺少工具契约: open_app"],
  }));
  assert.match(blocked.rejectionNotice ?? "", /缺少工具契约/);
});

test("回滚证据：rolled_back 回执绑定 from→to 版本与 digest", () => {
  const v = updateControlView(state({
    receipts: [{
      apply_id: "apply_1", action: "rolled_back",
      release_id: "r1", version: "2.0.0", artifact_digest: "cc",
      outcome: "ok", reason: "健康检查失败",
      to_release_id: "r1", attempts: 1, last_attempt_at: 42,
    }],
  }));
  assert.deepEqual(v.rollbackEvidence, {
    fromVersion: "2.0.0", toVersion: "r1", digest: "cc", at: 42,
  });
  const none = updateControlView(state());
  assert.equal(none.rollbackEvidence, null);
});
