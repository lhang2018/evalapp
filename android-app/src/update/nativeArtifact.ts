/**
 * React Native 端制品端口适配：原生文件系统 + HTTP Range。
 *
 * 网络与磁盘能力只通过 UpdateManager 注入的端口暴露；路径始终相对
 * 应用私有目录，避免 JS 侧传入绝对路径越界。
 */

import { NativeModules } from "react-native";

import type { ArtifactFileSystem } from "./artifact.ts";
import { base64ToBytes, bytesToBase64 } from "./base64.ts";
export { FetchArtifactSource } from "./rangeSource.ts";

interface NativeUpdateModule {
  stat(path: string): Promise<{ size: number } | null>;
  append(path: string, data: string): Promise<boolean>;
  read(path: string): Promise<string>;
  rename(from: string, to: string): Promise<boolean>;
  remove(path: string): Promise<boolean>;
  freeBytes(): Promise<number>;
}

function nativeModule(): NativeUpdateModule {
  const module = (NativeModules as Record<string, unknown>)
    .EvoAgentUpdate as NativeUpdateModule | undefined;
  if (!module) throw new Error("原生更新模块不可用");
  return module;
}

export class NativeArtifactFiles implements ArtifactFileSystem {
  private readonly module: NativeUpdateModule;

  constructor(module: NativeUpdateModule = nativeModule()) {
    this.module = module;
  }

  async stat(path: string): Promise<{ size: number } | null> {
    return this.module.stat(path);
  }

  async append(path: string, bytes: Uint8Array): Promise<void> {
    const ok = await this.module.append(path, bytesToBase64(bytes));
    if (!ok) throw new Error("写暂存文件失败");
  }

  async read(path: string): Promise<Uint8Array> {
    return base64ToBytes(await this.module.read(path));
  }

  async rename(from: string, to: string): Promise<void> {
    if (!(await this.module.rename(from, to))) {
      throw new Error("原子改名失败");
    }
  }

  async remove(path: string): Promise<void> {
    await this.module.remove(path);
  }

  async freeBytes(): Promise<number> {
    return this.module.freeBytes();
  }
}

