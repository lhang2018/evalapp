/** 原生文件选择器导入诊断包；模块缺失时给出明确错误，不静默降级。 */

import { NativeModules } from "react-native";

import { decodePickedPackage } from "./packageImport.ts";
import type {
  DiagnosticsPickResult,
  PickedPackage,
} from "./packageImport.ts";

export interface DiagnosticsPickerModule {
  pickDiagnosticsPackage?: () => Promise<DiagnosticsPickResult>;
}

export class NativeDiagnosticsPicker {
  private readonly module: DiagnosticsPickerModule | null;

  constructor(module?: DiagnosticsPickerModule | null) {
    this.module = module ??
      ((NativeModules as Record<string, unknown>).EvoAgentDevice as
        DiagnosticsPickerModule | undefined) ?? null;
  }

  async pick(): Promise<PickedPackage> {
    const pick = this.module?.pickDiagnosticsPackage;
    if (!pick) {
      throw new Error("当前构建不支持系统文件选择器");
    }
    return decodePickedPackage(await pick.call(this.module));
  }
}
