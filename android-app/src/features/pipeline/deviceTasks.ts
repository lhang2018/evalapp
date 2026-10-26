/** Foreground device-task orchestration; execution remains in the existing broker/ledger. */
import { createClientRequestId, createLocalTaskId } from "../../offline/ids.ts";
import { enqueueInto } from "../../offline/queue.ts";
import type { OfflineRepository } from "../../offline/storage.ts";
import type { LocalTask, OfflineState } from "../../offline/types.ts";
import {
  createProposal, executeProposal, receiptForUpload,
} from "../../device/taskProtocol.ts";
import type {
  ActionReceipt, DeviceActionProposal, DeviceBrokerLike, TaskActionLedger,
} from "../../device/taskProtocol.ts";
import { classifyPipeline } from "./chain.ts";
import { sha256Hex } from "../../offline/sha256.ts";

export class DeviceTaskService {
  private readonly repo: OfflineRepository;
  private readonly ledger: TaskActionLedger;
  private readonly broker: DeviceBrokerLike;
  private readonly now: () => number;
  private readonly runtimeManifestId: string | null;
  constructor(
    repo: OfflineRepository, ledger: TaskActionLedger, broker: DeviceBrokerLike,
    now: () => number = () => Date.now(), runtimeManifestId: string | null = null,
  ) {
    this.repo = repo;
    this.ledger = ledger;
    this.broker = broker;
    this.now = now;
    this.runtimeManifestId = runtimeManifestId;
  }

  /** Only exact supported requests are intercepted; other work uses normal planning. */
  async prepare(text: string): Promise<LocalTask | null> {
    const id = createLocalTaskId();
    const decision = classifyPipeline({ text, modeOverride: "work", task_id: id });
    if (!decision.proposalInput) return null;
    const at = this.now();
    const proposal = createProposal({ ...decision.proposalInput, now: at });
    return this.repo.update((state) => {
      const task: LocalTask = {
        local_task_id: id, client_request_id: createClientRequestId(),
        execution_target: "device", device_execution: { proposal, phase: "prepared" },
        current_run_id: proposal.run_id, server_task_id: null,
        goal: text.trim(), intent: "device_action", risk_level: proposal.risk,
        status: "WAITING_DEPENDENCY", result: null, failure_reason: null,
        created_at: at, updated_at: at, last_sync_error: null,
      };
      state.tasks.push(task);
      this.event(state, task, "device_task_prepared", { action_id: proposal.action_id });
      return task;
    });
  }

  private task(state: OfflineState, id: string): LocalTask {
    const task = state.tasks.find((row) => row.local_task_id === id);
    const p = task?.device_execution?.proposal;
    if (!task || task.execution_target !== "device" || !p || task.server_task_id
        || p.task_id !== id || p.run_id !== task.current_run_id) {
      throw new Error("本机任务与设备提议身份不一致");
    }
    const expected = typeof task.goal === "string" ? classifyPipeline({ text: task.goal,
      modeOverride: "work", task_id: id, run_id: task.current_run_id ?? undefined }).proposalInput : null;
    if (!expected || task.intent !== "device_action" || task.risk_level !== "low"
        || p.run_id !== `local:${id}` || p.risk !== "low" || !Number.isFinite(p.created_at)
        || [p.proposal_id, p.action_id, p.effect_id].some((value) => typeof value !== "string" || !value.trim())
        || p.kind !== expected.kind || p.target !== expected.target
        || JSON.stringify(p.args === undefined ? {} : p.args) !== JSON.stringify(expected.args)
        || JSON.stringify(p.expected_observation) !== JSON.stringify(expected.expected_observation)) {
      throw new Error("保存的设备动作或任务确认范围已变化，禁止执行");
    }
    return task;
  }

  private checkReceipt(receipt: ActionReceipt, p: DeviceActionProposal): void {
    if (receipt.action_id !== p.action_id || receipt.task_id !== p.task_id || receipt.run_id !== p.run_id
        || receipt.effect_id !== p.effect_id || receipt.proposal_id !== p.proposal_id
        || receipt.kind !== p.kind || receipt.risk !== p.risk) throw new Error("本机回执身份不一致，禁止继续");
  }

  private event(state: OfflineState, task: LocalTask, type: string, payload: Record<string, unknown>) {
    enqueueInto(state, { event_type: type, task_id: task.local_task_id,
      run_id: task.current_run_id, runtime_manifest_id: this.runtimeManifestId, payload }, this.now());
  }

  async execute(id: string, confirm: (proposal: DeviceActionProposal) => Promise<boolean>,
                foreground: () => boolean = () => true): Promise<ActionReceipt | null> {
    const before = this.task(await this.repo.load(), id);
    if (before.device_execution!.phase !== "prepared") return this.refresh(id);
    const proposal = before.device_execution!.proposal;
    const identity = JSON.stringify(proposal);
    if ((await confirm(JSON.parse(identity))) !== true) return null;
    if (!foreground()) throw new Error("请回到应用后重新确认本机操作");
    const claimed = await this.repo.update((state) => {
      const task = this.task(state, id);
      if (JSON.stringify(task.device_execution!.proposal) !== identity) throw new Error("确认期间操作已变化，请重新核对");
      if (task.device_execution!.phase !== "prepared") return false;
      task.device_execution!.phase = "claimed";
      task.updated_at = this.now();
      return true;
    });
    if (!claimed) return this.refresh(id);
    // A crash after this durable claim never turns back into an executable task.
    if (!foreground()) throw new Error("操作已暂存，未继续下发，请读取回执核对");
    const result = await executeProposal(this.ledger, this.broker, proposal, { now: this.now });
    await this.refresh(id);
    return result.receipt;
  }

  /** Reads/publishes an existing receipt only; never calls the device executor. */
  async refresh(id: string): Promise<ActionReceipt | null> {
    const task = this.task(await this.repo.load(), id);
    const p = task.device_execution!.proposal;
    return this.ledger.withReceipt(p.action_id, async (receipt) => {
      if (!receipt) return null;
      this.checkReceipt(receipt, p);
      await this.repo.update((state) => {
        const current = this.task(state, id);
        const execution = current.device_execution!;
        if (JSON.stringify(execution.proposal) !== JSON.stringify(p)) throw new Error("设备提议已变化");
        if (["claimed", "needs_review", "awaiting_acceptance"].includes(execution.phase)) {
          execution.phase = receipt.state === "verified" ? "awaiting_acceptance" : "needs_review";
        }
        const projection = receiptForUpload(receipt);
        const signature = JSON.stringify(projection);
        if (execution.queued_receipt !== signature) {
          this.event(state, current, "device_action_receipt_verification", { ...projection });
          execution.queued_receipt = signature;
        }
        current.updated_at = this.now();
      });
      return receipt;
    });
  }

  async accept(id: string, confirmed: boolean): Promise<void> {
    if (confirmed !== true) throw new Error("任务成果需由你明确确认");
    const receipt = await this.refresh(id);
    if (!receipt || receipt.state !== "verified" || receipt.terminal !== true) {
      throw new Error("设备操作尚未核验，不能确认任务完成");
    }
    const signature = JSON.stringify(receiptForUpload(receipt));
    await this.ledger.withReceipt(receipt.action_id, async (current) => {
      if (!current || current.state !== "verified" || !current.terminal
          || JSON.stringify(receiptForUpload(current)) !== signature) throw new Error("设备回执已更新，请重新核对成果");
      await this.repo.update((state) => {
        const task = this.task(state, id);
        this.checkReceipt(current, task.device_execution!.proposal);
        if (task.device_execution!.phase === "accepted") return;
        if (task.device_execution!.phase !== "awaiting_acceptance"
            || task.device_execution!.proposal.action_id !== receipt.action_id) throw new Error("任务不处于成果确认阶段");
        task.device_execution!.phase = "accepted";
        task.device_execution!.accepted_receipt = signature;
        task.status = "DONE";
        task.result = "你已确认：系统设置已打开";
        task.updated_at = this.now();
        this.event(state, task, "device_task_accepted", { action_id: receipt.action_id,
          effect_id: receipt.effect_id, receipt_sha256: sha256Hex(signature) });
      });
    });
  }

  async cancel(id: string): Promise<void> {
    await this.repo.update((state) => {
      const task = this.task(state, id);
      if (task.device_execution!.phase === "cancelled") return;
      if (task.device_execution!.phase !== "prepared") throw new Error("操作已发起，不能撤销已发生的动作，请先核对回执");
      task.device_execution!.phase = "cancelled";
      task.status = "DONE";
      task.failure_reason = "你已取消本机操作";
      task.updated_at = this.now();
      this.event(state, task, "device_task_cancelled", { action_id: task.device_execution!.proposal.action_id });
    });
  }
}
