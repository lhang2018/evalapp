/**
 * 活动能力包的执行消费与健康探针（ALN-11-W）。
 *
 * 职责：把已装载（已验签）的活动 bundle 内嵌 workflow 真正执行起来，
 * 并用有界自动探针的真实执行结果驱动更新管理器的健康判定与回滚；
 * 不以切槽成功或展示新版本替代新能力执行。
 *
 * 边界：
 * - workflow_def 是服务端 WorkflowDef.to_dict() 的可序列化子集
 *   （仅工具节点），不引入另一套工作流格式。
 * - 下载内容不 eval：节点只能引用客户端注册的工具，参数是纯数据。
 * - 设备动作由 App 注入的工具实现走既有 DeviceBroker 授权/副作用边界。
 */

import type { KeyValueStore } from "../offline/storage.ts";
import { createId } from "../offline/ids.ts";
import { loadActiveBundle, validateWorkflowDef } from "./workflowBundle.ts";
import type {
  BundleWorkflowDef, BundleWorkflowNode, InstalledBundle,
} from "./workflowBundle.ts";

export type {
  BundleWorkflowDef, BundleWorkflowNode,
} from "./workflowBundle.ts";

export interface BundleHealthProbe {
  input?: Record<string, unknown>;
  expect_output_contains?: string;
  expect_output_equals?: unknown;
}

// ---- 执行前拒绝（验收：不兼容/未注册工具/权限不足/非法 workflow）----

export type BundleRejectCode =
  | "no_active_bundle"
  | "not_executable"
  | "incompatible"
  | "tool_unregistered"
  | "permission_missing"
  | "illegal_workflow";

export class BundleRejectedError extends Error {
  readonly code: BundleRejectCode;
  readonly reasons: string[];

  constructor(code: BundleRejectCode, reasons: string[]) {
    super(reasons.join("; ") || code);
    this.name = "BundleRejectedError";
    this.code = code;
    this.reasons = reasons;
  }
}

// ---- 执行与探针记录（记录持久化，重启后版本关联不丢失）----

export interface BundleToolContext {
  task_ref: string;
  node_id: string;
  /** 上游节点输出，按 node_id 索引 */
  outputs: Record<string, unknown>;
}

export type BundleTool = (
  args: Record<string, unknown>,
  ctx: BundleToolContext,
) => Promise<unknown>;

export interface BundleExecutionStep {
  node_id: string;
  tool: string;
  state: "success" | "failed";
  attempts: number;
  started_at: number;
  finished_at: number;
  output?: unknown;
  error?: string;
}

export interface BundleBinding {
  release_id: string;
  version: string;
  digest: string;
  workflow_id: string;
  workflow_version: string;
}

export interface BundleExecutionRecord {
  execution_id: string;
  task_ref: string;
  release_id: string;
  version: string;
  digest: string;
  workflow_id: string;
  workflow_version: string;
  input: Record<string, unknown>;
  output: unknown;
  steps: BundleExecutionStep[];
  outcome: "succeeded" | "failed" | "timeout";
  started_at: number;
  finished_at: number;
}

export type ProbeOutcome = "healthy" | "unhealthy" | "unknown";

export interface BundleProbeRecord {
  probe_id: string;
  outcome: ProbeOutcome;
  release_id: string;
  version: string;
  digest: string;
  workflow_id: string;
  task_ref: string;
  reason: string;
  execution_id: string | null;
  at: number;
}

export interface BundleRecoveryRecord {
  at: number;
  rollback_reason: string;
  failed: BundleBinding | null;
  recovered: BundleBinding | null;
  execution_id: string | null;
  outcome: "succeeded" | "failed" | "timeout" | "not_executable";
}

export interface BundleRuntimeState {
  version: 1;
  binding: BundleBinding | null;
  records: BundleExecutionRecord[];
  probes: BundleProbeRecord[];
  recovery: BundleRecoveryRecord | null;
}

export interface BundleExecutable {
  bundle: InstalledBundle;
  def: BundleWorkflowDef;
  probe: BundleHealthProbe | null;
}

const RUNTIME_STATE_KEY = "bundle_runtime_v1";
const MAX_RECORDS = 20;
const DEFAULT_EXECUTION_TIMEOUT_MS = 10_000;

export function emptyRuntimeState(): BundleRuntimeState {
  return { version: 1, binding: null, records: [], probes: [], recovery: null };
}

function parseRuntimeState(raw: string | null): BundleRuntimeState {
  if (!raw) return emptyRuntimeState();
  try {
    const data = JSON.parse(raw) as Partial<BundleRuntimeState>;
    if (!data || data.version !== 1) return emptyRuntimeState();
    return {
      version: 1,
      binding: data.binding ?? null,
      records: Array.isArray(data.records) ? data.records : [],
      probes: Array.isArray(data.probes) ? data.probes : [],
      recovery: data.recovery ?? null,
    };
  } catch {
    return emptyRuntimeState();
  }
}

/** 与 manager.ts 相同的语义化版本比较；bundle 兼容声明复用同规则。 */
export function versionAtLeast(current: string, minimum: string): boolean {
  const parse = (value: string): number[] | null => {
    const match = /^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$/i.exec(value.trim());
    if (!match) return null;
    const parts = [match[1], match[2] ?? "0", match[3] ?? "0"].map(Number);
    return parts.every(Number.isSafeInteger) ? parts : null;
  };
  const left = parse(current);
  const right = parse(minimum);
  if (!left || !right) return false;
  for (let index = 0; index < 3; index += 1) {
    if (left[index] > right[index]) return true;
    if (left[index] < right[index]) return false;
  }
  return true;
}

/** 极简 JSON-schema 子集：type + required，够校验节点输出契约。 */
function schemaProblems(
  value: unknown,
  schema: Record<string, unknown> | undefined,
  label: string,
): string[] {
  if (!schema || typeof schema !== "object") return [];
  const problems: string[] = [];
  const expected = schema.type;
  if (typeof expected === "string" && expected !== "any") {
    const actual = Array.isArray(value)
      ? "array"
      : value === null
        ? "null"
        : typeof value;
    if (expected === "integer" ? !Number.isInteger(value) : actual !== expected) {
      problems.push(`${label} 输出类型 ${actual} 不符 ${expected}`);
    }
  }
  const required = schema.required;
  if (Array.isArray(required) && value && typeof value === "object"
      && !Array.isArray(value)) {
    const record = value as Record<string, unknown>;
    for (const key of required) {
      if (!(key in record)) problems.push(`${label} 缺少必需输出 ${key}`);
    }
  }
  return problems;
}

export interface BundleRuntimeOptions {
  appVersion: string;
  tools: Record<string, BundleTool>;
  grantedPermissions?: () => string[] | Promise<string[]>;
  now?: () => number;
  executionTimeoutMs?: number;
  storeKey?: string;
}

/**
 * 执行消费入口：App 构造一次，任务路径与探针共用。
 * 每次执行都从 KV 重新装载活动包——回滚后立即生效，不缓存旧版本。
 */
export class BundleRuntime {
  private readonly kv: KeyValueStore;
  private readonly appVersion: string;
  private readonly tools: Record<string, BundleTool>;
  private readonly grantedPermissions: () => string[] | Promise<string[]>;
  private readonly now: () => number;
  private readonly executionTimeoutMs: number;
  private readonly storeKey: string;

  constructor(kv: KeyValueStore, options: BundleRuntimeOptions) {
    this.kv = kv;
    this.appVersion = options.appVersion;
    this.tools = options.tools;
    this.grantedPermissions = options.grantedPermissions ?? (() => []);
    this.now = options.now ?? (() => Date.now());
    this.executionTimeoutMs
      = options.executionTimeoutMs ?? DEFAULT_EXECUTION_TIMEOUT_MS;
    this.storeKey = options.storeKey ?? RUNTIME_STATE_KEY;
  }

  async loadState(): Promise<BundleRuntimeState> {
    return parseRuntimeState(await this.kv.getItem(this.storeKey));
  }

  /** 活动包是否声明消费该意图（任务路径据此选择执行方式）。 */
  async declaresIntent(intent: string): Promise<boolean> {
    const bundle = await this.loadActiveBundle();
    if (!bundle) return false;
    return typeof bundle.bundle.intent === "string"
      && bundle.bundle.intent === intent;
  }

  /**
   * 解析并完整校验当前活动包的可执行体。任何一项不满足都以
   * BundleRejectedError 在执行前拒绝；不做部分放行。
   */
  async resolveExecutable(): Promise<BundleExecutable> {
    const bundle = await this.loadActiveBundle();
    if (!bundle) {
      throw new BundleRejectedError("no_active_bundle", ["当前没有活动能力包"]);
    }
    if (!bundle.bundle.workflow_def) {
      throw new BundleRejectedError("not_executable",
        ["活动包未内嵌可执行 workflow 定义"]);
    }
    const problems: string[] = [];
    if (typeof bundle.bundle.min_app_version === "string"
        && !versionAtLeast(this.appVersion, bundle.bundle.min_app_version)) {
      problems.push(
        `能力包要求 App >= ${bundle.bundle.min_app_version}，当前 ${this.appVersion}`);
    }
    const required = Array.isArray(bundle.bundle.required_permissions)
      ? (bundle.bundle.required_permissions as string[])
      : [];
    if (required.length) {
      const granted = await this.grantedPermissions();
      const missing = required.filter((name) => !granted.includes(name));
      if (missing.length) {
        problems.push(`缺少权限: ${missing.join(", ")}`);
      }
    }
    if (problems.length) {
      throw new BundleRejectedError(
        problems.some((p) => p.startsWith("能力包要求 App"))
          ? "incompatible"
          : "permission_missing",
        problems);
    }
    const def = bundle.bundle.workflow_def as BundleWorkflowDef;
    const defProblems = validateWorkflowDef(def);
    if (defProblems.length) {
      throw new BundleRejectedError("illegal_workflow", defProblems);
    }
    if (def.workflow_id !== bundle.bundle.workflow) {
      throw new BundleRejectedError("illegal_workflow",
        [`workflow_def.workflow_id (${def.workflow_id}) 与包标识 (${bundle.bundle.workflow}) 不一致`]);
    }
    const unregistered = [...new Set(def.nodes.map((n) => n.tool))]
      .filter((tool) => !(tool in this.tools));
    if (unregistered.length) {
      throw new BundleRejectedError("tool_unregistered",
        [`未注册工具: ${unregistered.join(", ")}`]);
    }
    return { bundle, def, probe: this.readProbe(bundle) };
  }

  /**
   * 执行活动包 workflow：按依赖拓扑执行工具节点，输出契约不符按失败处理。
   * 成败都落执行记录（绑定 release/version/digest 与 task_ref）。
   */
  async execute(
    input: Record<string, unknown>,
    taskRef: string,
  ): Promise<BundleExecutionRecord> {
    const { bundle, def } = await this.resolveExecutable();
    const startedAt = this.now();
    const record: BundleExecutionRecord = {
      execution_id: createId("bexec"),
      task_ref: taskRef,
      release_id: bundle.release_id,
      version: bundle.version,
      digest: bundle.digest,
      workflow_id: def.workflow_id,
      workflow_version: def.version,
      input: { ...input },
      output: null,
      steps: [],
      outcome: "failed",
      started_at: startedAt,
      finished_at: startedAt,
    };
    const timedOut = await this.runDag(def, input, record, taskRef);
    record.finished_at = this.now();
    record.outcome = timedOut
      ? "timeout"
      : record.steps.every((s) => s.state === "success")
        ? "succeeded"
        : "failed";
    if (record.outcome === "succeeded") {
      const outputs = Object.fromEntries(
        record.steps.map((s) => [s.node_id, s.output]),
      );
      const last = record.steps[record.steps.length - 1];
      record.output = last ? last.output : outputs;
      const outputProblems = def.output_schema
        ? schemaProblems(record.output, def.output_schema, "workflow")
        : [];
      if (outputProblems.length) {
        record.outcome = "failed";
        record.steps.push({
          node_id: "__output_schema__", tool: "(contract)", state: "failed",
          attempts: 0, started_at: record.finished_at,
          finished_at: record.finished_at,
          error: outputProblems.join("; "),
        });
      }
    }
    await this.persist((state) => {
      state.binding = {
        release_id: record.release_id, version: record.version,
        digest: record.digest, workflow_id: record.workflow_id,
        workflow_version: record.workflow_version,
      };
      state.records = [...state.records, record].slice(-MAX_RECORDS);
    });
    return record;
  }

  /**
   * 有界自动健康探针：真实执行活动包 workflow 并核对期望。
   * 执行前拒绝/执行失败/期望不符 = unhealthy；超时/无结果 = unknown，
   * 两种情况都不标健康；只有 healthy 才允许晋级。
   */
  async runHealthProbe(taskRef?: string): Promise<BundleProbeRecord> {
    const at = this.now();
    const probeId = createId("probe");
    const ref = taskRef ?? probeId;
    const fail = async (
      outcome: ProbeOutcome, reason: string,
      executionId: string | null = null,
      binding: Partial<BundleBinding> = {},
    ): Promise<BundleProbeRecord> => {
      const record: BundleProbeRecord = {
        probe_id: probeId, outcome,
        release_id: binding.release_id ?? "unknown",
        version: binding.version ?? "unknown",
        digest: binding.digest ?? "unknown",
        workflow_id: binding.workflow_id ?? "unknown",
        task_ref: ref, reason, execution_id: executionId, at: this.now(),
      };
      await this.persist((state) => {
        state.probes = [...state.probes, record].slice(-MAX_RECORDS);
      });
      return record;
    };

    let executable: BundleExecutable;
    try {
      executable = await this.resolveExecutable();
    } catch (error) {
      if (error instanceof BundleRejectedError) {
        // 已激活但执行前被拒：确定性的新版本故障，不是无结果
        return fail("unhealthy", `执行前拒绝(${error.code}): ${error.reasons.join("; ")}`);
      }
      return fail("unknown", `探针异常: ${messageOf(error)}`);
    }
    const bundle = executable.bundle;
    const binding = {
      release_id: bundle.release_id, version: bundle.version,
      digest: bundle.digest, workflow_id: executable.def.workflow_id,
    };
    const probeInput = executable.probe?.input ?? {};
    let record: BundleExecutionRecord;
    try {
      record = await this.withTimeout(
        this.execute(probeInput, ref),
        this.executionTimeoutMs,
      );
    } catch (error) {
      const timedOut = messageOf(error) === "执行超时";
      return fail(timedOut ? "unknown" : "unhealthy",
        timedOut ? "探针执行超时（无结果，不标健康）" : `探针执行失败: ${messageOf(error)}`,
        null, binding);
    }
    if (record.outcome === "timeout") {
      return fail("unknown", "探针执行超时（无结果，不标健康）",
        record.execution_id, binding);
    }
    if (record.outcome !== "succeeded") {
      const stepError = record.steps.find((s) => s.state === "failed")?.error;
      return fail("unhealthy", `探针执行失败: ${stepError ?? "workflow 未成功"}`,
        record.execution_id, binding);
    }
    const expectation = this.checkExpectation(executable.probe, record.output);
    if (expectation) {
      return fail("unhealthy", `探针期望不符: ${expectation}`,
        record.execution_id, binding);
    }
    return fail("healthy", "探针执行成功且期望相符",
      record.execution_id, binding);
  }

  /**
   * 回滚恢复验证：回滚后对当前（原健康）包用同一探针输入真实执行一次，
   * 证明旧能力确实可执行；结果与失败版本的版本关联一并落库。
   */
  async verifyRecovery(rollbackReason: string, taskRef?: string): Promise<BundleRecoveryRecord> {
    const state = await this.loadState();
    const lastUnhealthy = [...state.probes]
      .reverse()
      .find((p) => p.outcome === "unhealthy");
    const failed: BundleBinding | null = lastUnhealthy
      ? {
        release_id: lastUnhealthy.release_id,
        version: lastUnhealthy.version,
        digest: lastUnhealthy.digest,
        workflow_id: lastUnhealthy.workflow_id,
        workflow_version: "unknown",
      }
      : state.binding;
    const at = this.now();
    try {
      const record = await this.withTimeout(
        this.execute(await this.probeInputOfActive(), taskRef ?? createId("recover")),
        this.executionTimeoutMs,
      );
      const recovery: BundleRecoveryRecord = {
        at, rollback_reason: rollbackReason, failed,
        recovered: {
          release_id: record.release_id, version: record.version,
          digest: record.digest, workflow_id: record.workflow_id,
          workflow_version: record.workflow_version,
        },
        execution_id: record.execution_id,
        outcome: record.outcome === "succeeded" ? "succeeded" : record.outcome,
      };
      await this.persist((current) => { current.recovery = recovery; });
      return recovery;
    } catch (error) {
      const recovery: BundleRecoveryRecord = {
        at, rollback_reason: rollbackReason, failed, recovered: null,
        execution_id: null,
        outcome: error instanceof BundleRejectedError
          ? "not_executable"
          : "timeout",
      };
      await this.persist((current) => { current.recovery = recovery; });
      return recovery;
    }
  }

  // ---- 内部 ----

  private async loadActiveBundle(): Promise<InstalledBundle | null> {
    return loadActiveBundle(this.kv);
  }

  private readProbe(bundle: InstalledBundle): BundleHealthProbe | null {
    const probe = bundle.bundle.health_probe;
    if (!probe || typeof probe !== "object" || Array.isArray(probe)) return null;
    return probe as BundleHealthProbe;
  }

  private async probeInputOfActive(): Promise<Record<string, unknown>> {
    const executable = await this.resolveExecutable();
    return executable.probe?.input ?? {};
  }

  private checkExpectation(
    probe: BundleHealthProbe | null, output: unknown,
  ): string | null {
    if (!probe) return null;
    if (typeof probe.expect_output_contains === "string"
        && !stringify(output).includes(probe.expect_output_contains)) {
      return `输出不包含 "${probe.expect_output_contains}"`;
    }
    if (probe.expect_output_equals !== undefined
        && stringify(probe.expect_output_equals) !== stringify(output)) {
      return `输出不等于期望值 ${stringify(probe.expect_output_equals)}`;
    }
    return null;
  }

  /** 拓扑执行 DAG；返回是否超时。失败节点的下游不执行。 */
  private async runDag(
    def: BundleWorkflowDef,
    input: Record<string, unknown>,
    record: BundleExecutionRecord,
    taskRef: string,
  ): Promise<boolean> {
    const deadline = this.now() + this.executionTimeoutMs;
    const nodes = [...def.nodes];
    const done = new Map<string, BundleExecutionStep>();
    const outputs: Record<string, unknown> = {};
    const remaining = () => nodes.filter((n) => !done.has(n.node_id));
    while (remaining().length) {
      const ready = remaining().filter((node) =>
        (node.deps ?? []).every((dep) => done.get(dep)?.state === "success"));
      if (!ready.length) {
        // 依赖失败导致的不可达节点：按失败记录，阻断执行
        for (const node of remaining()) {
          record.steps.push({
            node_id: node.node_id, tool: node.tool, state: "failed",
            attempts: 0, started_at: this.now(), finished_at: this.now(),
            error: "上游依赖未成功，未执行",
          });
        }
        return false;
      }
      for (const node of ready) {
        if (this.now() > deadline) {
          record.steps.push({
            node_id: node.node_id, tool: node.tool, state: "failed",
            attempts: 0, started_at: this.now(), finished_at: this.now(),
            error: "执行超时",
          });
          return true;
        }
        const step = await this.runNode(node, input, outputs, taskRef, deadline);
        record.steps.push(step);
        done.set(node.node_id, step);
        if (step.state === "success") outputs[node.node_id] = step.output;
      }
    }
    return false;
  }

  private async runNode(
    node: BundleWorkflowNode,
    input: Record<string, unknown>,
    outputs: Record<string, unknown>,
    taskRef: string,
    deadline: number,
  ): Promise<BundleExecutionStep> {
    const attempts = Math.max(1, 1 + Math.max(0, node.max_retries ?? 0));
    const startedAt = this.now();
    const step: BundleExecutionStep = {
      node_id: node.node_id, tool: node.tool, state: "failed",
      attempts: 0, started_at: startedAt, finished_at: startedAt,
    };
    const runner = this.tools[node.tool];
    let lastError = "工具未注册";
    for (let attempt = 1; attempt <= attempts; attempt += 1) {
      if (this.now() > deadline) {
        lastError = "执行超时";
        break;
      }
      step.attempts = attempt;
      try {
        const output = await runner({
          ...(input ?? {}),
          ...(node.args ?? {}),
        }, { task_ref: taskRef, node_id: node.node_id, outputs: { ...outputs } });
        const problems = schemaProblems(output, node.output_schema, node.node_id);
        if (problems.length) {
          lastError = problems.join("; ");
          continue;
        }
        step.state = "success";
        step.output = output;
        step.error = undefined;
        break;
      } catch (error) {
        lastError = messageOf(error);
      }
    }
    step.finished_at = this.now();
    if (step.state !== "success") step.error = lastError;
    return step;
  }

  private async withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      return await Promise.race([
        promise,
        new Promise<never>((_, reject) => {
          timer = setTimeout(() => reject(new Error("执行超时")), ms);
        }),
      ]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  private async persist(
    mutate: (state: BundleRuntimeState) => void,
  ): Promise<void> {
    const state = await this.loadState();
    mutate(state);
    await this.kv.setItem(this.storeKey, JSON.stringify(state));
  }
}

function stringify(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value) ?? "";
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
