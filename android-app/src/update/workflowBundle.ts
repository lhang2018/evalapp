/**
 * 能力包（workflow bundle）装载：校验 JSON 结构后写入目标槽，
 * 并记录当前生效槽。只有已经过字节验签的制品才会进入这里。
 */

import type { ArtifactFileSystem } from "./artifact.ts";
import { sha256Hex } from "../offline/sha256.ts";
import { utf8Decode } from "./base64.ts";
import type { KeyValueStore } from "../offline/storage.ts";
import type { BundleApplyRequest, BundleApplier } from "./manager.ts";

export const ACTIVE_BUNDLE_KEY = "update_bundle_active_v1";
export const SLOT_BUNDLE_PREFIX = "update_bundle_v1:";

export interface InstalledBundle {
  release_id: string;
  version: string;
  slot: "a" | "b";
  digest: string;
  bundle_ref?: string | null;
  installed_at: number;
  bundle: Record<string, unknown>;
}

/** 包内嵌工作流定义：服务端 WorkflowDef.to_dict() 的可序列化子集（仅工具节点）。 */
export interface BundleWorkflowNode {
  node_id: string;
  description?: string;
  deps?: string[];
  tool: string;
  args?: Record<string, unknown>;
  max_retries?: number;
  timeout?: number;
  requires_confirmation?: boolean;
  output_schema?: Record<string, unknown>;
}

export interface BundleWorkflowDef {
  workflow_id: string;
  version: string;
  name?: string;
  input_schema?: Record<string, unknown>;
  output_schema?: Record<string, unknown>;
  nodes: BundleWorkflowNode[];
}

export function slotBundleKey(slot: "a" | "b"): string {
  return `${SLOT_BUNDLE_PREFIX}${slot}`;
}

function parseBundle(bytes: Uint8Array): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(utf8Decode(bytes));
  } catch {
    throw new Error("能力包不是合法 JSON");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("能力包必须是对象");
  }
  const bundle = parsed as Record<string, unknown>;
  if (typeof bundle.workflow !== "string" || !bundle.workflow.trim()) {
    throw new Error("能力包缺少 workflow 标识");
  }
  if (bundle.workflow_def !== undefined) {
    const problems = validateWorkflowDef(bundle.workflow_def);
    if (problems.length) {
      throw new Error(`能力包 workflow_def 非法: ${problems.join("; ")}`);
    }
    const def = bundle.workflow_def as { workflow_id?: unknown };
    if (def.workflow_id !== bundle.workflow) {
      throw new Error(
        `workflow_def.workflow_id (${String(def.workflow_id)}) 与包标识 (${bundle.workflow}) 不一致`);
    }
  }
  return bundle;
}

/**
 * 结构化 DAG 校验（与 evoagent/workflows/models.py WorkflowDef.validate
 * 同口径）：重复ID/未知依赖/环/无可执行体。装载与执行前都调用，
 * 非法 workflow 在成为活动包或执行前即被拒绝。
 */
export function validateWorkflowDef(def: unknown): string[] {
  if (!def || typeof def !== "object" || Array.isArray(def)) {
    return ["workflow_def 必须是对象"];
  }
  const candidate = def as Partial<BundleWorkflowDef>;
  if (typeof candidate.workflow_id !== "string"
      || !candidate.workflow_id.trim()) {
    return ["workflow_def 缺少 workflow_id"];
  }
  if (typeof candidate.version !== "string" || !candidate.version.trim()) {
    return ["workflow_def 缺少 version"];
  }
  if (!Array.isArray(candidate.nodes) || candidate.nodes.length === 0) {
    return ["workflow_def 至少需要一个节点"];
  }
  const problems: string[] = [];
  const ids = new Set<string>();
  for (const node of candidate.nodes) {
    if (!node || typeof node !== "object") {
      problems.push("存在非法节点");
      continue;
    }
    if (typeof node.node_id !== "string" || !node.node_id.trim()) {
      problems.push("存在缺少 node_id 的节点");
      continue;
    }
    if (ids.has(node.node_id)) problems.push(`节点ID重复: ${node.node_id}`);
    ids.add(node.node_id);
    if (typeof node.tool !== "string" || !node.tool.trim()) {
      problems.push(`节点 ${node.node_id} 未定义工具引用`);
    }
    if (node.args !== undefined
        && (!node.args || typeof node.args !== "object"
          || Array.isArray(node.args))) {
      problems.push(`节点 ${node.node_id} args 必须是对象`);
    }
    if (node.deps !== undefined) {
      if (!Array.isArray(node.deps)
          || node.deps.some((d) => typeof d !== "string")) {
        problems.push(`节点 ${node.node_id} deps 必须是字符串数组`);
      }
    }
  }
  if (problems.length) return problems;
  for (const node of candidate.nodes as BundleWorkflowNode[]) {
    for (const dep of node.deps ?? []) {
      if (!ids.has(dep)) {
        problems.push(`节点 ${node.node_id} 依赖不存在的 ${dep}`);
      }
    }
  }
  if (problems.length) return problems;
  // 环检测
  const nodeMap = new Map(
    (candidate.nodes as BundleWorkflowNode[]).map((n) => [n.node_id, n]),
  );
  const visited = new Set<string>();
  const stack = new Set<string>();
  const dfs = (nodeId: string): void => {
    if (stack.has(nodeId)) {
      problems.push(`存在依赖环: ${nodeId}`);
      return;
    }
    if (visited.has(nodeId)) return;
    stack.add(nodeId);
    for (const dep of nodeMap.get(nodeId)?.deps ?? []) dfs(dep);
    stack.delete(nodeId);
    visited.add(nodeId);
  };
  for (const nodeId of ids) dfs(nodeId);
  return problems;
}

export class WorkflowBundleApplier implements BundleApplier {
  private readonly fs: ArtifactFileSystem;
  private readonly kv: KeyValueStore;
  private readonly now: () => number;

  constructor(fs: ArtifactFileSystem, kv: KeyValueStore,
              now: () => number = () => Date.now()) {
    this.fs = fs;
    this.kv = kv;
    this.now = now;
  }

  async apply(request: BundleApplyRequest): Promise<void> {
    const bytes = await this.fs.read(request.path);
    if (sha256Hex(bytes) !== request.digest) {
      throw new Error("装载时制品摘要不符");
    }
    const bundle = parseBundle(bytes);
    const record: InstalledBundle = {
      release_id: request.release_id,
      version: request.version,
      slot: request.slot,
      digest: request.digest,
      bundle_ref: request.bundle_ref ?? null,
      installed_at: this.now(),
      bundle,
    };
    // 先写目标槽，再指向生效槽：中断时旧槽保持可用。
    await this.kv.setItem(slotBundleKey(request.slot),
                          JSON.stringify(record));
    await this.kv.setItem(ACTIVE_BUNDLE_KEY, JSON.stringify({
      slot: record.slot,
      release_id: record.release_id,
      version: record.version,
      digest: record.digest,
      installed_at: record.installed_at,
    }));
  }
}

export async function loadActiveBundle(
  kv: KeyValueStore,
): Promise<InstalledBundle | null> {  const raw = await kv.getItem(ACTIVE_BUNDLE_KEY);
  if (!raw) return null;
  let marker: { slot?: unknown } | null = null;
  try {
    marker = JSON.parse(raw) as { slot?: unknown };
  } catch {
    return null;
  }
  if (!marker || (marker.slot !== "a" && marker.slot !== "b")) return null;
  const stored = await kv.getItem(slotBundleKey(marker.slot));
  if (!stored) return null;
  try {
    return JSON.parse(stored) as InstalledBundle;
  } catch {
    return null;
  }
}
