/** 安装实例标识：首次生成后长期复用（双端协同 §3.1）。 */

import type { KeyValueStore } from "../offline/storage.ts";
import { createId } from "../offline/ids.ts";

export const INSTALLATION_KEY = "evoagent_installation_id";
const ID_RE = /^[A-Za-z0-9_.:-]{8,128}$/;

export async function getInstallationId(
  store: KeyValueStore,
): Promise<string> {
  const existing = (await store.getItem(INSTALLATION_KEY))?.trim();
  if (existing && ID_RE.test(existing)) return existing;
  const created = createId("inst");
  await store.setItem(INSTALLATION_KEY, created);
  return created;
}
