/**
 * 预算与费用用户可见投影（ALN-24-E）。
 *
 * 规则（任务卡验收）：
 * - unknown 不能当 0 金额：未知成本单独标注，不参与余额计算；
 * - 预算耗尽 = 停止新增调用（GLOBAL-03 断言），已生成产物保留；
 * - 调整额度不能绕过动作准入（额度变更走既有 15-A/15-R 权威，
 *   客户端只展示不修改）；
 * - 无依据不显示百分比（与 23-C 同口径）。
 *
 * 数据来自 /system 的 budget 字段（权威 BudgetController 快照），
 * 客户端不做二次预算计算。
 */

import type { LocalTask } from "../../offline/types.ts";
import { taskViewOf } from "../taskview/state.ts";

export interface BudgetUsageSnapshot {
  model_calls?: number;
  tool_calls?: number;
  cost?: number;
  seconds?: number;
  unknown_costs?: number;
}

export interface BudgetPeriod {
  runtime_day?: BudgetUsageSnapshot;
  runtime_month?: BudgetUsageSnapshot;
  evolution_day?: BudgetUsageSnapshot;
  evolution_month?: BudgetUsageSnapshot;
}

export interface SystemBudget {
  budget?: BudgetPeriod;
}

// ---- 用户可读格式 ----

export function formatCost(cost: number | undefined, currency?: string): string {
  if (typeof cost !== "number" || !Number.isFinite(cost) || cost < 0) return "—";
  const amount = cost.toFixed(3);
  if (currency === "CNY") return "¥" + amount;
  if (currency === "USD") return "$" + amount;
  if (currency && /^[A-Z]{3}$/.test(currency)) return currency + " " + amount;
  return amount + "（币种未知）";
}

export function formatCalls(calls: number | undefined): string {
  return `${calls ?? 0} 次调用`;
}

// ---- 未知成本：不能当 0 金额 ----

export interface UnknownCostView {
  /** true = 存在未知成本需要标注 */
  hasUnknown: boolean;
  /** 用户可见提示 */
  label: string;
}

export function unknownCostView(usage: BudgetUsageSnapshot | undefined): UnknownCostView {
  const count = usage?.unknown_costs ?? 0;
  if (count <= 0) return { hasUnknown: false, label: "" };
  return {
    hasUnknown: true,
    label: `${count} 项未知成本待对账（不按 0 元释放）`,
  };
}

// ---- 预算接近耗尽的用户提示 ----

export interface BudgetStatusView {
  /** ok | warning | exhausted */
  level: "ok" | "warning" | "exhausted" | "unknown";
  /** 用户可见文案 */
  label: string;
  /** true = 不再有新调用 */
  blocked: boolean;
  /** true = 接近上限需要提示 */
  warning: boolean;
  /** 独立核验的产物状态；null表示未核验，预算本身不能证明文件可访问。 */
  artifactsPreserved: boolean | null;
}

export function budgetStatusView(
  usage: BudgetUsageSnapshot | undefined,
  maxModelCalls: number | undefined,
  artifactsVerified: boolean | null = null,
): BudgetStatusView {
  const calls = usage?.model_calls ?? 0;
  const max = maxModelCalls;
  if (typeof max !== "number" || !Number.isFinite(max) || max < 0
      || !Number.isFinite(calls) || calls < 0 || usage === undefined) {
    return {
      level: "unknown", label: "预算信息未知，需要核验执行端额度",
      blocked: true, warning: true, artifactsPreserved: artifactsVerified,
    };
  }
  if (calls >= max) {
    return {
      level: "exhausted", label: artifactsVerified === true
        ? "预算耗尽：不再新增调用，已核验产物可用"
        : "预算耗尽：不再新增调用；产物可用性需独立核验",
      blocked: true, warning: false, artifactsPreserved: artifactsVerified,
    };
  }
  if (max > 0 && calls >= max * 0.8) {
    return {
      level: "warning", label: "预算接近上限",
      blocked: false, warning: true, artifactsPreserved: artifactsVerified,
    };
  }
  return { level: "ok", label: "预算充足", blocked: false, warning: false,
           artifactsPreserved: artifactsVerified };
}

// ---- 任务预算状态投影 ----

export interface TaskBudgetView {
  task_id: string;
  /** 用户可见预算状态 */
  budgetLabel: string;
  /** true = 需要用户动作（预算耗尽是其中一种） */
  needsUser: boolean;
}

export function taskBudgetView(task: LocalTask): TaskBudgetView {
  const view = taskViewOf(task);
  return {
    task_id: task.local_task_id,
    budgetLabel: view.label,
    needsUser: view.state === "needs_you",
  };
}
