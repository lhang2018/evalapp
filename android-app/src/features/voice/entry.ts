/**
 * 语音输入与文字任务共用入口（ALN-23-F）。
 *
 * 原则（任务卡）：
 * - 转写进入**同一任务契约**：语音只产出可编辑草稿，确认后仍走与文字输入
 *   完全相同的提交路径（goal → submit → TaskContractInput）；语音不新增
 *   第二条提交通道，也不携带 intent/risk/权限字段。
 * - 转写完成 ≠ 执行成功：转写只把文本填进输入框，绝不自动提交，也不建任务。
 * - 转写错误可修正：保留原始转写 transcript 供追溯，提交只取用户确认后的 text。
 * - 不旁路动作授权：入口只产出 goal，不覆盖风险等级；高风险外部动作照常由
 *   统一提交路径的动作授权判定。
 * - 声明识别/播报及离线范围：scope 明示可用性与离线依赖，缺失即显示不可用。
 */

import type {
  TranscriptionResult,
  VoiceCapability,
  VoiceStatus,
} from "../../device/voice.ts";

/** 草稿来源：voice=直接转写，voice_edited=用户在转写基础上修正，manual=无语音。 */
export type VoiceDraftSource = "voice" | "voice_edited" | "manual";

/**
 * 离线范围：系统语音识别通常由联机引擎提供，客户端无法保证断网可用。
 * - engine_dependent：识别可用，但离线可用性取决于系统引擎（不夸大承诺）。
 * - unavailable：识别不可用（缺依赖/未授权/无服务），离线自然也不可用。
 */
export type VoiceOfflineScope = "engine_dependent" | "unavailable";

export interface VoiceScopeDeclaration {
  /** 识别可用 */
  recognition: boolean;
  /** 播报可用 */
  speech: boolean;
  /** 麦克风权限已授权 */
  microphone: boolean;
  offline: VoiceOfflineScope;
  /** 用户可见说明（含离线范围） */
  note: string;
}

export interface VoiceEntry {
  /** 识别状态（沿用 voice.ts 六态） */
  status: VoiceStatus;
  /** 原始识别文本；仅用于修正追溯，永不直接提交 */
  transcript: string;
  /** 当前可编辑文本；提交只取这个 */
  text: string;
  /** 用户是否在转写基础上修正过 */
  edited: boolean;
  source: VoiceDraftSource;
  /** 识别结果说明（错误/空结果等直接展示） */
  message: string;
  scope: VoiceScopeDeclaration;
  /** 是否满足提交前置（识别成功且确认后仍有文本） */
  submittable: boolean;
}

/** 语音入口产出：与文字输入同源的提交目标，调用方必须走同一 submit 路径。 */
export interface VoiceTaskInput {
  ok: boolean;
  goal: string;
  reason: string | null;
}

/** 由原生能力投影出识别/播报/权限与离线范围声明。 */
export function describeVoiceScope(
  capability: VoiceCapability,
): VoiceScopeDeclaration {
  const recognition = capability.recognition_available;
  const speech = capability.tts_available;
  const microphone = capability.permission_granted;
  const offline: VoiceOfflineScope = recognition
    ? "engine_dependent"
    : "unavailable";
  const parts: string[] = [
    recognition ? "识别可用" : "识别不可用",
    speech ? "播报可用" : "播报不可用",
  ];
  if (recognition && !microphone) parts.push("麦克风未授权");
  parts.push(
    recognition
      ? "离线依赖系统识别引擎，断网可能不可用"
      : "缺依赖时明确显示不可用",
  );
  return { recognition, speech, microphone, offline, note: parts.join("；") };
}

function unavailableEntry(
  status: VoiceStatus,
  message: string,
  scope: VoiceScopeDeclaration,
): VoiceEntry {
  return {
    status: status === "transcribed" ? "empty" : status,
    transcript: "",
    text: "",
    edited: false,
    source: "manual",
    message,
    scope,
    submittable: false,
  };
}

/**
 * 把一次识别结果转成可编辑草稿。只有 transcribed 且有文本才可提交；
 * 空/取消/拒绝/不可用/失败一律不可提交，且不保留旧文本。
 */
export function applyTranscription(
  result: TranscriptionResult,
  capability: VoiceCapability,
): VoiceEntry {
  const scope = describeVoiceScope(capability);
  if (result.status !== "transcribed") {
    return unavailableEntry(result.status, result.message, scope);
  }
  const transcript = result.transcript.trim();
  if (!transcript) {
    return unavailableEntry("empty", result.message, scope);
  }
  return {
    status: "transcribed",
    transcript,
    text: transcript,
    edited: false,
    source: "voice",
    message: result.message,
    scope,
    submittable: true,
  };
}

/**
 * 修正转写：用户编辑草稿文本。提交只取修正后的 text；原始 transcript 保留。
 * 清空文本即视为不可提交（不会退回原始转写）。
 */
export function editVoiceEntry(entry: VoiceEntry, text: string): VoiceEntry {
  const next = typeof text === "string" ? text : "";
  if (entry.status !== "transcribed") {
    return { ...entry, text: next, submittable: false };
  }
  const edited = next !== entry.transcript;
  return {
    ...entry,
    text: next,
    edited,
    source: edited ? "voice_edited" : "voice",
    submittable: next.trim() !== "",
  };
}

/** 清除语音草稿：回到无语音状态，文本一并清空。 */
export function clearVoiceEntry(entry: VoiceEntry): VoiceEntry {
  return {
    ...entry,
    status: "empty",
    transcript: "",
    text: "",
    edited: false,
    source: "manual",
    message: "已清除语音输入",
    submittable: false,
  };
}

function failReason(entry: VoiceEntry | null): string {
  if (!entry) return "还没有语音输入";
  switch (entry.status) {
    case "denied":
      return "未授权麦克风，语音输入不可用";
    case "unavailable":
      return "语音服务不可用";
    case "cancelled":
      return "已取消语音输入";
    case "failed":
      return entry.message || "语音识别失败";
    case "empty":
      return entry.message || "没有识别到语音内容";
    case "transcribed":
      // transcribed 但不可提交 = 用户把文本清空了
      return "语音任务内容为空";
    default:
      return entry.message || "没有可提交的语音任务";
  }
}

/**
 * 桥接输出：转写草稿 → 提交目标（goal）。这里只产出文本，不做提交，
 * 也不返回 intent/risk/权限字段——授权与执行仍由统一提交路径负责。
 */
export function buildVoiceTaskInput(entry: VoiceEntry | null): VoiceTaskInput {
  if (!entry || !entry.submittable) {
    return { ok: false, goal: "", reason: failReason(entry) };
  }
  const goal = entry.text.trim();
  if (!goal) {
    return { ok: false, goal: "", reason: "语音任务内容为空" };
  }
  return { ok: true, goal, reason: null };
}

/** 输入框下方展示的短说明：强调"需确认后提交，不会自动执行"。 */
export function voiceEntrySummary(entry: VoiceEntry | null): string {
  if (!entry) return "";
  if (entry.status === "transcribed") {
    return entry.edited
      ? `已修正语音转写（原始：「${entry.transcript}」），确认后提交`
      : "语音转写已完成，确认后提交（不会自动执行）";
  }
  return entry.message;
}
