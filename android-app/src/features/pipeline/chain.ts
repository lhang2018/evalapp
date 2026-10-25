/** Device proposal preparation and receipt projection (ALN-12-E).
 * App/broker execution and artifact generation remain separate integration work.
 */
import type { LocalTask } from "../../offline/types.ts";
import { classifyMode, type ChatMode } from "../modes/transition.ts";
import type { ProposalInput, ActionReceipt } from "../../device/taskProtocol.ts";
import { isTerminalJournalState, receiptForUpload } from "../../device/taskProtocol.ts";
import { taskViewOf } from "../taskview/state.ts";

export interface TaskPipelineInput {
  text: string;
  modeOverride: ChatMode | null;
  task_id?: string;
  run_id?: string;
}
export interface PipelineDecision {
  mode: ChatMode;
  proposalInput: ProposalInput | null;
  /** Work that cannot form an exact supported action must enter task planning. */
  planningReason: string | null;
}

export function classifyPipeline(input: TaskPipelineInput): PipelineDecision {
  const mode = classifyMode(input.text, { explicit: input.modeOverride }).mode;
  if (mode === "chat") return { mode, proposalInput: null, planningReason: null };
  const settings = /^(?:(?:请你|请|帮我|替我)\s*)?(?:打开|启动)\s*(?:系统)?设置[。！!]?$/i.test(input.text.trim())
    || /^(?:please\s+)?(?:open|launch)\s+(?:system\s+)?settings[.!]?$/i.test(input.text.trim());
  if (!settings) return { mode, proposalInput: null, planningReason: "需要进一步规划或确认具体动作与目标" };
  if (!input.task_id?.trim()) return { mode, proposalInput: null, planningReason: "设备动作需要任务身份" };
  return { mode, planningReason: null, proposalInput: {
    task_id: input.task_id, run_id: input.run_id, kind: "launch_intent",
    target: "com.android.settings", args: {},
    expected_observation: { expect_package: "com.android.settings" },
  }};
}

export interface PipelineResult {
  state: string;
  /** Device action outcome; one action never establishes whole-task success. */
  label: string;
  taskLabel: string | null;
  uploadRefs: Record<string, unknown>;
  sideEffectWarning: string | null;
}
const LABELS: Record<ActionReceipt["state"], string> = {
  prepared: "设备操作已准备", dispatched: "设备操作已下发，等待核验",
  verified: "设备操作已核验", unverified: "设备操作结果未确认",
  failed: "设备操作失败", denied: "设备操作已被拒绝", unavailable: "设备服务不可用",
};
export function resultView(receipt: ActionReceipt, task?: LocalTask): PipelineResult {
  if (task && task.local_task_id !== receipt.task_id) throw new Error("回执与任务身份不一致");
  if (task?.current_run_id && task.current_run_id !== receipt.run_id) throw new Error("回执与当前运行身份不一致");
  const known = Object.prototype.hasOwnProperty.call(LABELS, receipt.state);
  const inconsistent = !known || receipt.terminal !== isTerminalJournalState(receipt.state);
  const warning = inconsistent ? "回执状态不一致，需核对，禁止自动重放"
    : receipt.state === "unverified" || receipt.state === "dispatched" ? "结果未确认，禁止自动重放"
    : receipt.state === "denied" ? "操作已被拒绝"
    : receipt.state === "unavailable" ? "设备服务不可用"
    : receipt.state === "failed" ? "设备操作失败" : null;
  return {
    state: receipt.state,
    label: inconsistent ? "设备操作状态待核对" : LABELS[receipt.state] ?? "设备操作状态未知",
    taskLabel: task ? taskViewOf(task).label : null,
    uploadRefs: { ...receiptForUpload(receipt) }, sideEffectWarning: warning,
  };
}

/** Upload only the existing minimal receipt projection, never raw evidence. */
export function uploadPayload(receipt: ActionReceipt): Record<string, unknown> {
  return { ...receiptForUpload(receipt) };
}
