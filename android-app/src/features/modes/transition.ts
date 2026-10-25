/**
 * 聊天/工作模式与上下文转换（ALN-23-B）。
 *
 * 原则（任务卡）：直接问答轻量完成；需要工具/产物时**复用上下文**进入
 * 正式任务，不复制执行内核。
 * - 模式判定：用户显式切换 > 输入特征（工具/产物/执行意图）> 默认聊天。
 * - 转换投影：把聊天会话蒸馏为任务种子（goal + 已确认约束 + 纠正 +
 *   已完成要点），供 23-A 任务契约消费——聊天里聊过≠已执行，种子不把
 *   讨论当步骤重跑。
 * - 已完成步骤不重复：completed 标记的要点只进 contextDigest 不进
 *   pendingSteps。
 */

export type ChatMode = "chat" | "work";

export type ModeSource = "user_explicit" | "input_signal" | "default";

export interface ModeDecision {
  mode: ChatMode;
  source: ModeSource;
  /** 触发 work 判定的信号（用户可见，不暴露内部词表） */
  signal: string | null;
}

/** 输入特征词表（客户端轻量版；与 guessIntent 互补，不替代服务端意图）。 */
const WORK_SIGNALS: Array<[RegExp, string]> = [
  [/帮我|替我|给我(做|弄|办|生成|整理|查)/, "请求代为执行"],
  [/文件|报告|文档|表格|视频|图片|保存|导出/, "需要产出产物"],
  [/打开|点击|发送|安装|设置(?!.*吗)|清理|整理/, "需要操作工具或设备"],
  [/自动化|定时|每天|每次|重复/, "需要可重复执行"],
  [/跑|执行|运行|测试|编译|分析(?!.*吗)/, "需要真实执行"],
];

const CHAT_SIGNALS: RegExp[] = [
  /^(什么是|为什么|怎么理解|解释|讲讲|聊聊|你觉得|帮我看看$)/,
  /[?？]\s*$/,
];

/** 疑问/解释型开场：概念问答保持轻量聊天（即使提到文件/运行等词）。 */
const QUESTION_STARTS = /^(什么是|为什么|如何|怎么|怎样|解释一下|解释|讲讲|聊聊|帮我解释|帮我看看|帮我理解|帮我对比一下原理|你觉得)/;
/** 复合执行句：解释之后接明确的执行段（然后/再/接着 + 生成/打开等）。 */
const EXECUTION_AFTER_SPLIT = /(然后|再|接着|同时)[^。？?]*?(帮我|生成|打开|保存|整理|发送|跑|执行)/;

export function classifyMode(
  input: string,
  opts: { explicit?: ChatMode | null } = {},
): ModeDecision {
  if (opts.explicit) {
    return { mode: opts.explicit, source: "user_explicit", signal: null };
  }
  const text = (input || "").trim();
  if (!text) {
    return { mode: "chat", source: "default", signal: null };
  }
  if (EXECUTION_AFTER_SPLIT.test(text)) {
    return { mode: "work", source: "input_signal", signal: "复合请求包含执行段" };
  }
  if (QUESTION_STARTS.test(text)) {
    return { mode: "chat", source: "input_signal", signal: null };
  }
  for (const [pat, label] of WORK_SIGNALS) {
    if (pat.test(text)) {
      return { mode: "work", source: "input_signal", signal: label };
    }
  }
  return { mode: "chat", source: "input_signal", signal: null };
}

/** 用户显式覆盖后，同会话内保持覆盖（除非再次显式切换）。 */
export function stickyMode(
  previous: ModeDecision,
  next: ChatMode | null,
): ChatMode | null {
  if (next) return next;
  if (previous.source === "user_explicit") return previous.mode;
  return null;
}

// ---- 聊天 → 工作：上下文转换投影 ----

export interface ChatTurn {
  role: "user" | "assistant";
  text: string;
  /** 用户确认过的约束（如“用中文”“不超过10页”被用户回‘可以’） */
  confirmed?: boolean;
  /** 用户对先前结论的纠正 */
  correction?: boolean;
  /** 该要点已在聊天中完成（讨论定案，不需要在任务里重做） */
  completed?: boolean;
}

export interface TaskSeed {
  goal: string;
  /** 已确认约束：进入任务后必须遵守 */
  constraints: string[];
  /** 纠正记录：任务应采用纠正后的口径，避免重犯 */
  corrections: string[];
  /** 已完成要点：仅作为背景摘要，不生成待执行步骤 */
  completedDigest: string[];
  /** 待办要点（来自聊天中明确要做但未做的事） */
  pendingPoints: string[];
}

/**
 * 从聊天轮次构建任务种子。规则：
 * - goal 取最后一条用户消息（显式目标）；若其纠正了先前目标，采用纠正口径。
 * - constraints/corrections 只收 confirmed/correction 标记的轮次——
 *   未确认的讨论不自动升级为约束。
 * - completed 轮次进 completedDigest，绝不进 pendingPoints。
 */
export function buildTaskSeed(turns: ChatTurn[]): TaskSeed {
  const users = turns.filter((t) => t.role === "user" && t.text.trim());
  // 后来明确的新目标覆盖过去的纠正；确认的约束是参数不是目标，
  // goal 取最后一条非约束的用户消息
  const goalCandidates = users.filter((t) => !t.confirmed);
  const goal = goalCandidates.length
    ? goalCandidates[goalCandidates.length - 1].text.trim()
    : (users[users.length - 1]?.text.trim() ?? "");
  const constraints: string[] = [];
  const corrections: string[] = [];
  const completedDigest: string[] = [];
  const pendingPoints: string[] = [];
  for (const t of turns) {
    const text = t.text.trim();
    if (!text) continue;
    if (t.correction) corrections.push(text);
    if (t.confirmed) constraints.push(text);
    if (t.completed) completedDigest.push(text);
    else if (t.role === "user" && !t.correction && !t.confirmed
             && text !== goal) {
      pendingPoints.push(text);
    }
  }
  return { goal, constraints, corrections, completedDigest, pendingPoints };
}

/** 转换守卫：种子中不得出现已完成要点重复执行。 */
export function noRepeatGuard(seed: TaskSeed): {
  ok: boolean;
  repeats: string[];
} {
  const done = new Set(seed.completedDigest.map((s) => s.trim()));
  const repeats = seed.pendingPoints.filter((p) => done.has(p.trim()));
  return { ok: repeats.length === 0, repeats };
}
