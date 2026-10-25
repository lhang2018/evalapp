/**
 * 能力状态用户投影（ALN-24-B）：把服务端六档状态与设备本地状态合并为
 * 用户可见的能力视图与恢复路径。避免"配置存在即显示可用"：
 * 拒绝/撤销永远显示为不可执行；unknown 如实展示为未知，不伪装可用。
 */

export interface ServerCapability {
  tool_id: string;
  version: string;
  state: "ready" | "permission_required" | "denied" |
         "dependency_missing" | "unverified" | "unregistered";
  reason: string;
  note?: string | null;
  high_risk?: boolean;
}

export interface CapabilitiesStatusPayload {
  capabilities: ServerCapability[];
  counts: Record<string, number>;
}

export interface LocalDeviceState {
  /** 无障碍服务是否可用（设备动作类能力的前置） */
  accessibilityAvailable: boolean;
}

export type UserCapabilityState =
  | "ready"
  | "needs_permission"
  | "needs_setup"
  | "blocked"
  | "unknown";

export interface UserCapabilityView {
  tool_id: string;
  version: string;
  state: UserCapabilityState;
  /** 用户可见原因 */
  reason: string;
  /** 恢复路径（null：无需动作） */
  recovery: string | null;
  /** true = 受保护动作当前不可执行 */
  blocked: boolean;
}

const SERVER_STATE_VIEW: Record<ServerCapability["state"], {
  state: UserCapabilityState;
  reason: string;
  recovery: string | null;
}> = {
  ready: { state: "ready", reason: "可用", recovery: null },
  permission_required: {
    state: "needs_permission",
    reason: "需要授权或确认后才能使用",
    recovery: "在任务执行时完成确认，或在权限页开启授权",
  },
  denied: {
    state: "blocked",
    reason: "已被策略拒绝，受保护动作不会执行",
    recovery: "如需使用，请联系管理员调整策略",
  },
  dependency_missing: {
    state: "needs_setup",
    reason: "依赖检查失败（fail-closed）",
    recovery: "检查依赖服务或网络后重试",
  },
  unverified: {
    state: "unknown",
    reason: "外部类服务，暂无健康探针",
    recovery: "以实际运行结果为准",
  },
  unregistered: {
    state: "blocked",
    reason: "能力未注册，不能执行",
    recovery: null,
  },
};

/** 设备动作类能力依赖无障碍服务；本机未授权时覆盖为待设置。 */
const ACCESSIBILITY_TOOLS = new Set(["open_app"]);

export function projectUserCapabilities(
  payload: CapabilitiesStatusPayload,
  local: LocalDeviceState,
): UserCapabilityView[] {
  return payload.capabilities.map((cap) => {
    const view = SERVER_STATE_VIEW[cap.state] ?? SERVER_STATE_VIEW.unverified;
    let state = view.state;
    let reason = cap.reason ? `${view.reason}：${cap.reason}` : view.reason;
    let recovery = view.recovery;
    let blocked = state === "blocked";
    if (ACCESSIBILITY_TOOLS.has(cap.tool_id) && !local.accessibilityAvailable
        && state !== "blocked") {
      state = "needs_setup";
      reason = "需要开启无障碍服务后才能操作手机";
      recovery = "到系统设置开启无障碍服务";
      blocked = true;
    }
    return { tool_id: cap.tool_id, version: cap.version,
             state, reason, recovery, blocked };
  });
}

/** 受保护动作守卫：非 ready 状态一律不允许发起执行。 */
export function canExecute(view: UserCapabilityView): boolean {
  return view.state === "ready";
}
