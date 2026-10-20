/** 可注入的键值存储与整文档快照（RN 侧用 AsyncStorage 适配）。 */

import type { OfflineState } from "./types.ts";

export interface KeyValueStore {
  getItem(key: string): Promise<string | null>;
  setItem(key: string, value: string): Promise<void>;
  removeItem(key: string): Promise<void>;
}

export class MemoryStore implements KeyValueStore {
  private data = new Map<string, string>();

  async getItem(key: string): Promise<string | null> {
    return this.data.has(key) ? (this.data.get(key) as string) : null;
  }

  async setItem(key: string, value: string): Promise<void> {
    this.data.set(key, value);
  }

  async removeItem(key: string): Promise<void> {
    this.data.delete(key);
  }
}

export const STATE_KEY = "offline_state_v1";

export function emptyState(): OfflineState {
  return {
    version: 1,
    queue: [],
    dead_letters: [],
    tasks: [],
    last_acked_seq: 0,
    next_seq: 0,
    last_sync_at: null,
  };
}

export function parseState(raw: string | null): OfflineState {
  if (!raw) return emptyState();
  try {
    const data = JSON.parse(raw) as Partial<OfflineState>;
    if (
      !data || data.version !== 1 ||
      !Array.isArray(data.queue) ||
      !Array.isArray(data.dead_letters) ||
      !Array.isArray(data.tasks)
    ) {
      return emptyState();
    }
    return { ...emptyState(), ...data };
  } catch {
    return emptyState();
  }
}

export class OfflineRepository {
  private chain: Promise<unknown> = Promise.resolve();
  private readonly kv: KeyValueStore;
  private readonly key: string;

  constructor(kv: KeyValueStore, key: string = STATE_KEY) {
    this.kv = kv;
    this.key = key;
  }

  async load(): Promise<OfflineState> {
    return parseState(await this.kv.getItem(this.key));
  }

  async save(state: OfflineState): Promise<void> {
    await this.kv.setItem(this.key, JSON.stringify(state));
  }

  /** 串行化读-改-写，避免并发覆盖。mutator 抛错时不落盘。 */
  update<T>(mutator: (state: OfflineState) => T | Promise<T>): Promise<T> {
    const run = async () => {
      const state = await this.load();
      const result = await mutator(state);
      await this.save(state);
      return result;
    };
    const next = this.chain.then(run, run);
    this.chain = next.catch(() => undefined);
    return next;
  }
}
