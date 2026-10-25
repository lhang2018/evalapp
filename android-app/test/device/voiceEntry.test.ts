import { test } from "node:test";
import assert from "node:assert/strict";

import {
  applyTranscription,
  buildVoiceTaskInput,
  clearVoiceEntry,
  describeVoiceScope,
  editVoiceEntry,
  voiceEntrySummary,
} from "../../src/features/voice/entry.ts";
import type { VoiceCapability } from "../../src/device/voice.ts";

const CAPABLE: VoiceCapability = {
  available: true,
  recognition_available: true,
  tts_available: true,
  permission_granted: true,
  recognition_engine: "com.example.asr",
  tts_engine: "com.example.tts",
};

const DEGRADED: VoiceCapability = {
  available: false,
  recognition_available: false,
  tts_available: false,
  permission_granted: false,
  recognition_engine: null,
  tts_engine: null,
};

test("转写完成不等于执行成功：只产出可提交 goal，不自动提交", () => {
  const entry = applyTranscription(
    { status: "transcribed", transcript: " 查询现在几点了 ", message: "已识别" },
    CAPABLE,
  );
  assert.equal(entry.status, "transcribed");
  assert.equal(entry.text, "查询现在几点了");
  assert.equal(entry.submittable, true);

  const bridged = buildVoiceTaskInput(entry);
  assert.equal(bridged.ok, true);
  assert.equal(bridged.goal, "查询现在几点了");
  // 桥接输出只有提交目标，不含 intent/risk/权限等旁路字段
  assert.deepEqual(Object.keys(bridged).sort(), ["goal", "ok", "reason"]);
});

test("非识别成功状态一律不可提交且不保留文本", () => {
  for (const status of ["empty", "cancelled", "denied", "unavailable", "failed"] as const) {
    const entry = applyTranscription(
      { status, transcript: "不该出现", message: "x" },
      DEGRADED,
    );
    assert.equal(entry.submittable, false, status);
    assert.equal(entry.text, "", status);
    const bridged = buildVoiceTaskInput(entry);
    assert.equal(bridged.ok, false, status);
    assert.equal(bridged.goal, "", status);
    assert.ok(bridged.reason, status);
  }
});

test("声称识别成功但文本为空按空结果处理", () => {
  const entry = applyTranscription(
    { status: "transcribed", transcript: "   ", message: "" },
    CAPABLE,
  );
  assert.equal(entry.status, "empty");
  assert.equal(entry.submittable, false);
  assert.equal(buildVoiceTaskInput(entry).ok, false);
});

test("转写错误可修正：提交取修正文本，原始转写保留", () => {
  const entry = applyTranscription(
    { status: "transcribed", transcript: "明天天气", message: "已识别" },
    CAPABLE,
  );
  const fixed = editVoiceEntry(entry, "查询明天天气");
  assert.equal(fixed.edited, true);
  assert.equal(fixed.source, "voice_edited");
  assert.equal(fixed.transcript, "明天天气");
  assert.equal(fixed.text, "查询明天天气");
  assert.equal(buildVoiceTaskInput(fixed).goal, "查询明天天气");
  assert.ok(voiceEntrySummary(fixed).includes("已修正"));

  // 改回原文即恢复为未修正
  const reverted = editVoiceEntry(fixed, "明天天气");
  assert.equal(reverted.edited, false);
  assert.equal(reverted.source, "voice");
});

test("修正后清空文本变为不可提交，不退回原始转写", () => {
  const entry = applyTranscription(
    { status: "transcribed", transcript: "有内容", message: "已识别" },
    CAPABLE,
  );
  const emptied = editVoiceEntry(entry, "   ");
  assert.equal(emptied.submittable, false);
  const bridged = buildVoiceTaskInput(emptied);
  assert.equal(bridged.ok, false);
  assert.equal(bridged.goal, "");
  assert.equal(bridged.reason, "语音任务内容为空");
});

test("清除语音草稿回到无语音且不可提交", () => {
  const entry = applyTranscription(
    { status: "transcribed", transcript: "内容", message: "已识别" },
    CAPABLE,
  );
  const cleared = clearVoiceEntry(entry);
  assert.equal(cleared.status, "empty");
  assert.equal(cleared.source, "manual");
  assert.equal(cleared.text, "");
  assert.equal(cleared.submittable, false);
  assert.equal(buildVoiceTaskInput(cleared).ok, false);
});

test("声明识别/播报及离线范围：可用时如实标注引擎相关", () => {
  const scope = describeVoiceScope(CAPABLE);
  assert.equal(scope.recognition, true);
  assert.equal(scope.speech, true);
  assert.equal(scope.microphone, true);
  assert.equal(scope.offline, "engine_dependent");
  assert.ok(scope.note.includes("离线"));
});

test("声明范围：缺依赖时离线与识别均标不可用", () => {
  const scope = describeVoiceScope(DEGRADED);
  assert.equal(scope.recognition, false);
  assert.equal(scope.speech, false);
  assert.equal(scope.offline, "unavailable");
  assert.ok(scope.note.includes("识别不可用"));
  assert.ok(scope.note.includes("不可用"));
});

test("识别可用但麦克风未授权：范围显式标注未授权", () => {
  const scope = describeVoiceScope({ ...CAPABLE, permission_granted: false });
  assert.equal(scope.microphone, false);
  assert.ok(scope.note.includes("麦克风未授权"));
});

test("空草稿桥接失败并给出原因，不产生提交目标", () => {
  assert.equal(buildVoiceTaskInput(null).ok, false);
  assert.equal(buildVoiceTaskInput(null).goal, "");
  const denied = applyTranscription(
    { status: "denied", transcript: "", message: "未授权麦克风" },
    DEGRADED,
  );
  assert.equal(buildVoiceTaskInput(denied).reason, "未授权麦克风，语音输入不可用");
});

test("重复录音以最后一次结果整体替换（不叠加）", () => {
  const first = applyTranscription(
    { status: "transcribed", transcript: "第一句", message: "已识别" },
    CAPABLE,
  );
  const second = applyTranscription(
    { status: "transcribed", transcript: "第二句", message: "已识别" },
    CAPABLE,
  );
  assert.equal(second.text, "第二句");
  assert.equal(second.transcript, "第二句");
  assert.equal(second.edited, false);
  assert.notEqual(second.text, first.text);
});
