/** 原生无障碍模块适配；模块缺失或不支持时返回 unavailable，不尝试绕过系统权限。 */

import { normalizeSnapshot } from "./observation.ts";
import type { DeviceSnapshot } from "./observation.ts";
import type { DeviceTransport } from "./transport.ts";
import type { DeviceAction, DeviceResult } from "./types.ts";

export interface NativeDeviceModuleLike {
  isServiceEnabled(): Promise<boolean>;
  launchIntent(target: string): Promise<{
    ok: boolean;
    evidence?: Record<string, unknown>;
    message?: string;
  }>;
  performClick(text: string): Promise<{
    ok: boolean;
    evidence?: Record<string, unknown>;
    message?: string;
  }>;
  snapshot?(): Promise<{
    ok: boolean;
    evidence?: Record<string, unknown>;
    message?: string;
  }>;
}

export class NativeDeviceTransport implements DeviceTransport {
  private readonly module: NativeDeviceModuleLike | null;

  constructor(module: NativeDeviceModuleLike | null) {
    this.module = module;
  }

  async isAvailable(): Promise<boolean> {
    if (!this.module) return false;
    try {
      return await this.module.isServiceEnabled();
    } catch {
      return false;
    }
  }

  /** 只读观测：不执行任何设备动作；模块或服务不可用时返回 null。 */
  async observe(): Promise<DeviceSnapshot | null> {
    if (!this.module?.snapshot) return null;
    try {
      const response = await this.module.snapshot();
      if (!response?.ok) return null;
      return normalizeSnapshot(response.evidence);
    } catch {
      return null;
    }
  }

  async dispatch(action: DeviceAction): Promise<DeviceResult> {
    if (!this.module) {
      return { status: "unavailable", message: "无障碍原生模块未安装" };
    }
    try {
      let response;
      if (action.kind === "launch_intent") {
        response = await this.module.launchIntent(action.target);
      } else if (action.kind === "click_text") {
        response = await this.module.performClick(action.target);
      } else {
        return {
          status: "failed",
          message: `不支持的动作类型: ${action.kind}`,
        };
      }
      if (!response) {
        return { status: "unverified", message: "原生模块无返回" };
      }
      if (!response.ok) {
        return {
          status: "failed",
          message: response.message ?? "系统拒绝执行",
        };
      }
      return {
        status: "verified",
        evidence: response.evidence,
        message: response.message,
      };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      return { status: "failed", message };
    }
  }
}
