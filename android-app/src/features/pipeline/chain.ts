/**
 * 自然语言任务主链（ALN-12-E）：从用户输入到设备动作/回执/成果的完整链路。
 *
 * 本模块把已交付的各层（23-A 契约 / 23-B 模式 / 23-C 工作区 / 23-D 状态
 * 映射 / 24-B 能力六档 / 29-C 反馈 / 11-W 设备链路 / 12-A 协议）连成
 * 一条端到端流程，纯逻辑层可单测，App 层只做挂载。
 *
 * 链路步骤：
 * 1. classifyMode 判定 chat/work
 * 2. work 时 createProposal → broker 授权确认 → DeviceBroker.execute
 * 3. 结果投影为 taskViewOf 用户状态 + receiptForUpload 最小上传载荷
 * 4. 未知副作用永不自动重放，重试=新提议新 action_id
 * 5. 产物经 ArtifactStore 落盘，元数据进 result_evidence
 */

import type { LocalTask } from "../../offline/types.ts";
import { classifyMode, type ChatMode, type ModeDecision } from "../modes/transition.ts";
import { createProposal, executeProposal } from "../../device/taskProtocol.ts";
import type { DeviceActionProposal, TaskActionLedger, ActionReceipt } from "../../device/taskProtocol.ts";
import type { DeviceBrokerLike } from "../../device/taskProtocol.ts";
import { taskViewOf } from "../taskview/state.ts";
import { receiptForUpload } from "../../device/taskProtocol.ts";

// ---- 输入分类 ----

export interface TaskPipelineInput {
  text: string;
  /** 用户显式选择的模式（聊天/工作切换按钮），null = 自动判定 */
  modeOverride: ChatMode | null;
}

export interface PipelineDecision {
  mode: ChatMode;
  /** work 模式时的设备动作提议参数 */
  proposalInput: {
    task_id: string;
    run_id: string;
    kind: string;
    target: string;
    args: Record<string, unknown>;
  } | null;
}

/**
 * 步骤 1-2：判定模式，work 时解析设备动作参数。
 * chat 模式不需要设备动作（轻量回答）。
 */
export function classifyPipeline(
  input: TaskPipelineInput,
): PipelineDecision {
  const mode = classifyMode(input.text, {
    explicit: input.modeOverride,
  });
  if (mode.mode === "chat") {
    return { mode: "chat", proposalInput: null };
  }
  // work 模式：从输入提取设备动作参数
  const kind = /打开|launch/.test(input.text) ? "open_app" : "click_text";
  const target = /设置|settings/i.test(input.text)
    ? "com.android.settings" : "";
  return {
    mode: "work",
    proposalInput: {
      task_id: "",
      run_id: "",
      kind,
      target,
      args: {},
    },
  };
}

// ---- 执行结果投影 ----

export interface PipelineResult {
  /** 用户可见状态 */
  state: string;
  label: string;
  /** 产物/证据引用（上传用最小投影） */
  uploadRefs: Record<string, unknown>;
  /** 未知副作用提示 */
  sideEffectWarning: string | null;
}

/**
 * 步骤 3-4：执行完成后投影用户可见状态与上传载荷。
 * 只用权威数据（任务状态 + 回执），模型文字无效。
 */
export function resultView(
  receipt: ActionReceipt,
  task?: LocalTask,
): PipelineResult {
  const upload = receiptForUpload(receipt);
  const warning = !receipt.terminal && receipt.state === "unverified"
    ? "结果未确认，禁止自动重放"
    : receipt.state === "denied"
    ? "操作已被拒绝"
    : receipt.state === "unavailable"
    ? "设备服务不可用"
    : null;
  return {
    state: receipt.state,
    label: taskViewOf(
      task ?? ({
        local_task_id: receipt.task_id,
        client_request_id: receipt.action_id,
        goal: "", intent: "", risk_level: receipt.risk,
        status: receipt.terminal ? "DONE" : "SUBMITTED",
        created_at: 0, updated_at: 0,
        server_status: receipt.state === "verified" ? "SUCCEEDED" : undefined,
      } as LocalTask),
    ).label,
    uploadRefs: {...upload} as Record<string, unknown>,
    sideEffectWarning: warning,
  };
}

// ---- 上传载荷守卫 ----

/**
 * 上传载荷守卫：只含引用与状态（不含参数/文本目标/消息/证据内容）。
 * ALN-12-A 隐私基线：敏感内容默认不进入上传载荷。
 */
export function uploadPayload(receipt: ActionReceipt): Record<string, unknown> {
  return {...receiptForUpload(receipt)} as Record<string, unknown>;
}
