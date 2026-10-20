/** 诊断包导入的纯函数部分：结果解码、大小限制与 HTTP 头文件名清洗。 */

import { base64Decode } from "./bytes.ts";

export const MAX_IMPORT_BYTES = 20 * 1024 * 1024;

export interface DiagnosticsPickResult {
  ok?: boolean;
  file_name?: string;
  size?: number;
  base64?: string;
  message?: string;
}

export interface PickedPackage {
  fileName: string;
  bytes: Uint8Array;
}

/** 原生选择器结果 → 字节；取消、空包、超限与非法 base64 均给出可读错误。 */
export function decodePickedPackage(
  result: DiagnosticsPickResult | null | undefined,
): PickedPackage {
  if (!result || !result.ok) {
    throw new Error(result?.message || "已取消选择");
  }
  if (typeof result.size === "number" && result.size > MAX_IMPORT_BYTES) {
    throw new Error("导入包超过 20 MiB");
  }
  const bytes = base64Decode(result.base64 ?? "");
  if (!bytes || bytes.length === 0) {
    throw new Error("导入包为空或无法解码");
  }
  if (bytes.length > MAX_IMPORT_BYTES) {
    throw new Error("导入包超过 20 MiB");
  }
  return {
    fileName: result.file_name || "diagnostics.zip",
    bytes,
  };
}

/** HTTP 头只能携带 ASCII；保留可读部分，路径分隔符与引号替换为下划线。 */
export function headerFileName(name: string): string {
  const cleaned = name
    .replace(/[\\/\"]/g, "_")
    .replace(/[^\x20-\x7e]/g, "_")
    .slice(0, 128);
  return cleaned.trim() || "diagnostics.zip";
}
