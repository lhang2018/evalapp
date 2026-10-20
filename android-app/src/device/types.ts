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
}

export interface DeviceResult {
  status: DeviceStatus;
  evidence?: Record<string, unknown>;
  message?: string;
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
}
