/**
 * 记忆控制视图（ALN-30-B）：把 ALN-24-D 的记忆控制端点投影为用户可读行，
 * 并把删除结果中的"保留边界/失败项/是否完全擦除"如实展示。
 *
 * 规则：
 * - 删除结果 `fully_erased` 恒为 false 时，必须展示保留层（墓碑/审计/备份）；
 * - 范围策略的"禁用"= 从分析范围 deny；空策略视为全部允许；
 * - 离线/权限拒绝必须有明确提示，不把失败当成成功。
 */

import type {
  MemoryControlItem,
  MemoryDeleteResult,
  MemoryScopePolicy,
} from "../../api.ts";

const STATUS_LABELS: Record<string, string> = {
  candidate: "候选",
  verified: "已核实",
  restricted: "受限",
  pending_review: "待复核",
  revoked: "已撤销",
};

const SCOPE_LABELS: Record<string, string> = {
  user: "用户",
  project: "项目",
  session: "会话",
  task: "任务",
  expert: "专家",
  model: "模型",
  shared: "共享",
};

const SOURCE_LABELS: Record<string, string> = {
  user: "用户提供",
  model: "模型生成",
  tool: "工具产出",
  eval: "评测产出",
  runtime: "运行时",
};

export interface MemoryRowView {
  memory_id: string;
  content: string;
  sourceLabel: string;
  scopeLabel: string;
  statusLabel: string;
  provenanceNote: string;
  canCorrect: boolean;
  canDelete: boolean;
}

export function buildMemoryRows(items: MemoryControlItem[]): MemoryRowView[] {
  return items.map((item) => {
    const source = item.provenance?.source ?? item.source ?? "";
    const scope = item.provenance?.scope ?? item.scope ?? "";
    return {
      memory_id: item.memory_id,
      content: item.content,
      sourceLabel: SOURCE_LABELS[source] ?? (source || "未知来源"),
      scopeLabel: SCOPE_LABELS[scope] ?? (scope || "未知范围"),
      statusLabel: STATUS_LABELS[item.status] ?? item.status,
      provenanceNote: item.provenance?.task_id
        ? "关联任务 " + item.provenance.task_id
        : "内存记录（来源：" + (source || "未知") + "）",
      canCorrect: item.status !== "revoked",
      canDelete: true,
    };
  });
}

export interface DeletionReportView {
  ok: boolean;
  title: string;
  text: string;
  retainedLines: string[];
  failureLines: string[];
  fullyErased: boolean;
  scopeClaim: string;
}

export function deletionReport(result: MemoryDeleteResult): DeletionReportView {
  const retained = (result.retained ?? []).map(
    (layer) => `${layer.layer}：${layer.note}`,
  );
  const failures = (result.failures ?? []).map(
    (item) => `${item.stage}：${item.error}`,
  );
  if (!result.ok) {
    return {
      ok: false,
      title: "删除失败",
      text: result.error || (failures.join("；") || "删除未生效"),
      retainedLines: retained,
      failureLines: failures,
      fullyErased: false,
      scopeClaim: result.scope_claim ?? "",
    };
  }
  const erased = result.fully_erased === true;
  const body = erased
    ? "已完全擦除（仅限本地记忆库）"
    : "已从本地记忆库删除；以下内容按策略保留，不宣称全域擦除";
  return {
    ok: true,
    title: erased ? "删除完成" : "已删除（有保留项）",
    text: body,
    retainedLines: retained,
    failureLines: failures,
    fullyErased: erased,
    scopeClaim: result.scope_claim ?? "",
  };
}

export function memoryControlError(
  error: { status?: number; message?: string } | null,
  online: boolean,
): string | null {
  if (!online) return "离线：记忆控制需要连接服务端";
  if (!error) return null;
  if (error.status === 401) return "需要管理员令牌（未授权）";
  if (error.status === 403) return "当前令牌无权限调整记忆（需要 admin）";
  return error.message || "记忆请求失败";
}

export interface ScopePolicyView {
  allow: string[];
  deny: string[];
  disabled: string[];
  text: string;
  manageable: boolean;
}

export function scopePolicyView(
  policy: MemoryScopePolicy | null,
): ScopePolicyView {
  const allow = [...(policy?.allow ?? [])];
  const deny = [...(policy?.deny ?? [])];
  return {
    allow,
    deny,
    disabled: deny,
    text: deny.length
      ? "已禁用范围：" + deny.map((s) => SCOPE_LABELS[s] ?? s).join("、")
      : "当前所有范围均参与分析",
    manageable: true,
  };
}

export function deniedScopesAfter(
  policy: MemoryScopePolicy | null,
  scope: string,
  disable: boolean,
): { ok: boolean; deny: string[]; error: string | null } {
  const known = new Set(policy?.scope_values ?? Object.keys(SCOPE_LABELS));
  if (!known.has(scope)) {
    return { ok: false, deny: [...(policy?.deny ?? [])], error: "未知范围" };
  }
  const current = new Set(policy?.deny ?? []);
  if (disable) current.add(scope);
  else current.delete(scope);
  const deny = [...current];
  const allow = (policy?.allow ?? []).filter((item) => !deny.includes(item));
  if (allow.length === 0 && deny.length === 0) {
    return { ok: false, deny, error: "至少保留一个范围参与分析" };
  }
  return { ok: true, deny, error: null };
}
