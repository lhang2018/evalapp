/** 设备操作契约（PRD 64-68）：结果必须可验证，高风险动作需独立确认。 */

export type DeviceRisk = "low" | "medium" | "high";

export type DeviceStatus =
  | "verified"
  | "unverified"
  | "failed"
  | "denied"
  | "unavailable";

export type JournalState =
  | "prepared"
  | "dispatched"
  | "verified"
  | "failed"
  | "denied"
  | "unverified"
  | "unavailable";

export interface DeviceAction {
  action_id: string;
  /** launch_intent | click_text | send_message | payment | delete ... */
  kind: string;
  target: string;
  args?: Record<string, unknown>;
  risk?: DeviceRisk;
  /** 任务链路关联（ALN-12-A 增量，可选）：由任务协议层填充，随日志留痕 */
  task_id?: string;
  run_id?: string | null;
  effect_id?: string;
}

export interface DeviceResult {
  status: DeviceStatus;
  evidence?: Record<string, unknown>;
  message?: string;
  /** Broker-owned fact for this invocation; transport responses cannot set it. */
  dispatch_attempted?: boolean;
}

export interface DeviceDispatchOptions {
  canDispatch?: () => boolean;
}

export interface ActionJournalEntry {
  action_id: string;
  kind: string;
  risk: DeviceRisk;
  state: JournalState;
  created_at: number;
  updated_at: number;
  evidence?: Record<string, unknown>;
  message?: string;
  /** 任务链路关联（ALN-12-A 增量，可选）：与动作同源，不改变既有语义 */
  task_id?: string;
  run_id?: string | null;
  effect_id?: string;
}
