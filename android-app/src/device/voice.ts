/**
 * 语音输入与语音反馈契约（PRD 64/69/89.1）。
 *
 * 原生模块缺失或系统服务不可用时统一降级为 unavailable，不抛异常；
 * 识别文本只是任务输入，是否提交由用户决定。
 */

export const MIN_VOICE_TIMEOUT_MS = 3_000;
export const MAX_VOICE_TIMEOUT_MS = 30_000;
export const DEFAULT_VOICE_TIMEOUT_MS = 10_000;
export const MAX_SPEECH_CHARS = 1_200;

export type VoiceStatus =
  | "transcribed"
  | "empty"
  | "cancelled"
  | "denied"
  | "unavailable"
  | "failed";

export type SpeakStatus = "spoken" | "cancelled" | "unavailable" | "failed";

export interface VoiceCapability {
  available: boolean;
  recognition_available: boolean;
  tts_available: boolean;
  permission_granted: boolean;
  recognition_engine: string | null;
  tts_engine: string | null;
}

export interface TranscriptionResult {
  status: VoiceStatus;
  transcript: string;
  message: string;
}

export interface SpeakResult {
  status: SpeakStatus;
  message: string;
}

export interface NativeVoiceModuleLike {
  voiceStatus(): Promise<Record<string, unknown>>;
  startVoiceInput(timeoutMs: number): Promise<Record<string, unknown>>;
  speak(text: string): Promise<Record<string, unknown>>;
  cancelVoiceInput?(): Promise<unknown>;
  stopSpeaking?(): Promise<unknown>;
}

const VOICE_STATUSES = new Set<VoiceStatus>([
  "transcribed",
  "empty",
  "cancelled",
  "denied",
  "unavailable",
  "failed",
]);

const SPEAK_STATUSES = new Set<SpeakStatus>([
  "spoken",
  "cancelled",
  "unavailable",
  "failed",
]);

const DEFAULT_MESSAGES: Record<VoiceStatus, string> = {
  transcribed: "已识别语音",
  empty: "没有识别到语音内容",
  cancelled: "已取消语音输入",
  denied: "未授权麦克风",
  unavailable: "语音服务不可用",
  failed: "语音识别失败",
};

export function clampVoiceTimeout(
  value?: number,
  fallback: number = DEFAULT_VOICE_TIMEOUT_MS,
): number {
  const base = typeof value === "number" && Number.isFinite(value) ? value : fallback;
  return Math.round(
    Math.min(MAX_VOICE_TIMEOUT_MS, Math.max(MIN_VOICE_TIMEOUT_MS, base)),
  );
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object"
    ? (value as Record<string, unknown>)
    : {};
}

function asText(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function engineName(value: unknown): string | null {
  const text = asText(value).trim();
  return text ? text : null;
}

export function normalizeVoiceCapability(raw: unknown): VoiceCapability {
  const source = asRecord(raw);
  const recognition = source.recognition_available === true;
  const tts = source.tts_available === true;
  return {
    available: source.available === true || recognition || tts,
    recognition_available: recognition,
    tts_available: tts,
    permission_granted: source.permission_granted === true,
    recognition_engine: engineName(source.recognition_engine),
    tts_engine: engineName(source.tts_engine),
  };
}

export function normalizeTranscription(raw: unknown): TranscriptionResult {
  const source = asRecord(raw);
  const status = VOICE_STATUSES.has(source.status as VoiceStatus)
    ? (source.status as VoiceStatus)
    : "failed";
  let transcript = asText(source.transcript).trim();
  let resolved = status;
  if (resolved === "transcribed" && !transcript) {
    // 原生层声称识别成功却没有文本：按空结果处理，避免提交空任务
    resolved = "empty";
  }
  if (resolved !== "transcribed") transcript = "";
  const message = asText(source.message).trim() || DEFAULT_MESSAGES[resolved];
  return { status: resolved, transcript, message };
}

export function normalizeSpeakResult(raw: unknown): SpeakResult {
  const source = asRecord(raw);
  const status = SPEAK_STATUSES.has(source.status as SpeakStatus)
    ? (source.status as SpeakStatus)
    : "failed";
  const message =
    asText(source.message).trim() ||
    (status === "spoken" ? "已朗读" : "语音合成失败");
  return { status, message };
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** 朗读前的文本规整：折叠空白并截断，避免整段长文一次性塞给 TTS。 */
export function prepareSpeechText(
  text: unknown,
  maxChars: number = MAX_SPEECH_CHARS,
): string {
  const normalized = asText(text).replace(/\s+/g, " ").trim();
  if (!normalized) return "";
  const limit = Math.max(1, Math.round(maxChars));
  return normalized.length > limit ? normalized.slice(0, limit) : normalized;
}

const UNAVAILABLE_CAPABILITY: VoiceCapability = {
  available: false,
  recognition_available: false,
  tts_available: false,
  permission_granted: false,
  recognition_engine: null,
  tts_engine: null,
};

export class VoiceService {
  private readonly module: NativeVoiceModuleLike | null;

  constructor(module: NativeVoiceModuleLike | null) {
    this.module = module;
  }

  async status(): Promise<VoiceCapability> {
    if (!this.module) return { ...UNAVAILABLE_CAPABILITY };
    try {
      return normalizeVoiceCapability(await this.module.voiceStatus());
    } catch {
      return { ...UNAVAILABLE_CAPABILITY };
    }
  }

  async transcribe(timeoutMs?: number): Promise<TranscriptionResult> {
    if (!this.module) {
      return {
        status: "unavailable",
        transcript: "",
        message: "语音原生模块未安装",
      };
    }
    try {
      return normalizeTranscription(
        await this.module.startVoiceInput(clampVoiceTimeout(timeoutMs)),
      );
    } catch (error) {
      return { status: "failed", transcript: "", message: errorMessage(error) };
    }
  }

  async cancel(): Promise<void> {
    try {
      await this.module?.cancelVoiceInput?.();
    } catch {
      // 取消是尽力而为：原生识别器可能已经结束
    }
  }

  async speak(text: unknown): Promise<SpeakResult> {
    const prepared = prepareSpeechText(text);
    if (!prepared) {
      return { status: "failed", message: "没有可朗读的内容" };
    }
    if (!this.module) {
      return { status: "unavailable", message: "语音原生模块未安装" };
    }
    try {
      return normalizeSpeakResult(await this.module.speak(prepared));
    } catch (error) {
      return { status: "failed", message: errorMessage(error) };
    }
  }

  async stop(): Promise<void> {
    try {
      await this.module?.stopSpeaking?.();
    } catch {
      // 停止是尽力而为
    }
  }
}
