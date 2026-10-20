import type { DeviceAction, DeviceResult } from "./types.ts";

export interface DeviceTransport {
  isAvailable(): Promise<boolean>;
  dispatch(action: DeviceAction): Promise<DeviceResult>;
}
