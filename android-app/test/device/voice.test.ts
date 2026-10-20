import { test } from "node:test";
import assert from "node:assert/strict";

import {
  VoiceService,
  clampVoiceTimeout,
  normalizeSpeakResult,
  normalizeTranscription,
  normalizeVoiceCapability,
  prepareSpeechText,
} from "../../src/device/voice.ts";
import type { NativeVoiceModuleLike } from "../../src/device/voice.ts";

class FakeVoiceModule implements NativeVoiceModuleLike {
  statusRaw: Record<string, unknown> = {
    available: true,
    recognition_available: true,
    tts_available: true,
    permission_granted: true,
    recognition_engine: "com.example.asr",
    tts_engine: "com.example.tts",
  };
  transcriptionRaw: Record<string, unknown> = {
    status: "transcribed",
    transcript: "  查询现在几点了  ",
    message: "已识别语音",
  };
  speakRaw: Record<string, unknown> = { status: "spoken", message: "已朗读" };
  failStatus = false;
  failSpeak = false;
  lastTimeout = 0;
  lastText = "";
  cancelCalls = 0;
  stopCalls = 0;

  async voiceStatus() {
    if (this.failStatus) throw new Error("status failed");
    return this.statusRaw;
  }

  async startVoiceInput(timeoutMs: number) {
    this.lastTimeout = timeoutMs;
    return this.transcriptionRaw;
  }

  async speak(text: string) {
    this.lastText = text;
    if (this.failSpeak) throw new Error("speak failed");
    return this.speakRaw;
  }

  async cancelVoiceInput() {
    this.cancelCalls += 1;
  }

  async stopSpeaking() {
    this.stopCalls += 1;
  }
}

test("missing native module degrades to unavailable without throwing", async () => {
  const service = new VoiceService(null);
  const capability = await service.status();
  assert.equal(capability.available, false);
  assert.equal(capability.recognition_available, false);
  assert.equal(capability.tts_available, false);

  const transcription = await service.transcribe();
  assert.equal(transcription.status, "unavailable");
  assert.equal(transcription.transcript, "");

  const speech = await service.speak("你好");
  assert.equal(speech.status, "unavailable");

  await service.cancel();
  await service.stop();
});

test("native capability is normalized with engine names", async () => {
  const module = new FakeVoiceModule();
  const capability = await new VoiceService(module).status();
  assert.equal(capability.available, true);
  assert.equal(capability.recognition_available, true);
  assert.equal(capability.tts_available, true);
  assert.equal(capability.permission_granted, true);
  assert.equal(capability.recognition_engine, "com.example.asr");
  assert.equal(capability.tts_engine, "com.example.tts");
});

test("capability failure is treated as unavailable", async () => {
  const module = new FakeVoiceModule();
  module.failStatus = true;
  const capability = await new VoiceService(module).status();
  assert.equal(capability.available, false);
  assert.equal(capability.recognition_engine, null);
});

test("timeout is clamped to the supported window", () => {
  assert.equal(clampVoiceTimeout(undefined), 10_000);
  assert.equal(clampVoiceTimeout(Number.NaN), 10_000);
  assert.equal(clampVoiceTimeout(500), 3_000);
  assert.equal(clampVoiceTimeout(120_000), 30_000);
  assert.equal(clampVoiceTimeout(8_000), 8_000);
});

test("transcription trims text and forwards the clamped timeout", async () => {
  const module = new FakeVoiceModule();
  const result = await new VoiceService(module).transcribe(60_000);
  assert.equal(result.status, "transcribed");
  assert.equal(result.transcript, "查询现在几点了");
  assert.equal(module.lastTimeout, 30_000);
});

test("empty result never carries stale transcript", () => {
  const result = normalizeTranscription({
    status: "transcribed",
    transcript: "   ",
    message: "",
  });
  assert.equal(result.status, "empty");
  assert.equal(result.transcript, "");
  assert.equal(result.message, "没有识别到语音内容");
});

test("denied and unknown statuses are mapped safely", () => {
  const denied = normalizeTranscription({
    status: "denied",
    transcript: "不该出现",
    message: "未授权麦克风",
  });
  assert.equal(denied.status, "denied");
  assert.equal(denied.transcript, "");

  const unknown = normalizeTranscription({ status: "exploded" });
  assert.equal(unknown.status, "failed");
  assert.equal(unknown.message, "语音识别失败");
});

test("module exceptions become failed with the error message", async () => {
  const module = new FakeVoiceModule();
  module.failSpeak = true;
  const result = await new VoiceService(module).speak("朗读我");
  assert.equal(result.status, "failed");
  assert.equal(result.message, "speak failed");
});

test("speech text collapses whitespace and caps length", () => {
  assert.equal(prepareSpeechText("  你好\n\n 世界  "), "你好 世界");
  assert.equal(prepareSpeechText("   "), "");
  assert.equal(prepareSpeechText("abcdef", 3), "abc");
  assert.equal(prepareSpeechText(undefined), "");
});

test("speak sends prepared text to the native module", async () => {
  const module = new FakeVoiceModule();
  const result = await new VoiceService(module).speak("  第一行\n第二行  ");
  assert.equal(result.status, "spoken");
  assert.equal(module.lastText, "第一行 第二行");
});

test("empty speech input fails without calling the module", async () => {
  const module = new FakeVoiceModule();
  const result = await new VoiceService(module).speak("   ");
  assert.equal(result.status, "failed");
  assert.equal(module.lastText, "");
});

test("normalizeSpeakResult maps unknown statuses to failed", () => {
  assert.equal(normalizeSpeakResult({ status: "spoken" }).status, "spoken");
  assert.equal(normalizeSpeakResult({ status: "weird" }).status, "failed");
  assert.equal(
    normalizeSpeakResult({ status: "cancelled", message: "已停止" }).message,
    "已停止",
  );
  assert.equal(
    normalizeVoiceCapability({ recognition_available: true }).available,
    true,
  );
});

test("cancel and stop delegate to the native module", async () => {
  const module = new FakeVoiceModule();
  const service = new VoiceService(module);
  await service.cancel();
  await service.stop();
  assert.equal(module.cancelCalls, 1);
  assert.equal(module.stopCalls, 1);
});
