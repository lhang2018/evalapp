/** RN 侧导出交付：字节先留在内存，用户选定目标后再原子写入。 */

import { NativeModules } from "react-native";

import { base64Encode } from "./bytes.ts";
import type { DiagnosticsExportSink } from "./export.ts";

interface SaveResult {
  ok?: boolean;
  message?: string;
  uri?: string;
}

export interface DiagnosticsSaveModule {
  saveDiagnosticsPackage?: (
    fileName: string,
    base64: string,
  ) => Promise<SaveResult>;
}

export class NativeExportSink implements DiagnosticsExportSink {
  private pending = new Map<string, string>();
  private counter = 0;
  private readonly module: DiagnosticsSaveModule | null;

  constructor(module?: DiagnosticsSaveModule | null) {
    this.module = module ??
      ((NativeModules as Record<string, unknown>).EvoAgentDevice as
        DiagnosticsSaveModule | undefined) ?? null;
  }

  async writeTemp(fileName: string, bytes: Uint8Array): Promise<string> {
    this.counter += 1;
    const path = `memory://diagnostics/${this.counter}/${fileName}`;
    this.pending.set(path, base64Encode(bytes));
    return path;
  }

  async deliver(path: string, fileName: string): Promise<void> {
    const save = this.module?.saveDiagnosticsPackage;
    if (!save) {
      throw new Error("当前构建不支持系统文件选择器");
    }
    const data = this.pending.get(path);
    if (!data) throw new Error("导出数据已失效，请重新导出");
    const result = await save.call(this.module, fileName, data);
    if (!result || !result.ok) {
      throw new Error(result?.message || "保存失败");
    }
    this.pending.delete(path);
  }

  async removeTemp(path: string): Promise<void> {
    this.pending.delete(path);
  }
}
