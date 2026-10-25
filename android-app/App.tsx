/**
 * EvoAgent Android 客户端主界面（PRD 89 / 64-76）。
 *
 * - 离线优先任务：本地落盘 → 在线后以 client_request_id 幂等提交
 * - 事件队列：断点续传、去重、死信、连续 checkpoint
 * - 无障碍操作：显式授权、结果可验证、高风险独立确认
 *
 * UI 可通过 OTA（CodePush / Expo Updates）整包推送更新。
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  AppState,
  Button,
  KeyboardAvoidingView,
  Linking,
  NativeModules,
  PermissionsAndroid,
  Platform,
  SafeAreaView,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { api } from "./src/api.ts";
import { ApprovalPanel } from "./src/features/approvals/ApprovalPanel.tsx";
import type {
  CandidateSummary,
  MemoryEntry,
  MetricsSnapshot,
  ModelsInfo,
  ReleaseVerify,
  ReleasesInfo,
  VersionInfo,
  WorkflowCenterDetail,
  WorkflowSummary,
} from "./src/api.ts";
import { asyncStorageStore } from "./src/storage/asyncStorage.ts";
import { OfflineRepository } from "./src/offline/storage.ts";
import { EventQueue } from "./src/offline/queue.ts";
import { LocalTaskService } from "./src/offline/tasks.ts";
import { ClientTransport } from "./src/offline/apiTransport.ts";
import { SyncEngine } from "./src/offline/sync.ts";
import { DiagnosticsExporter } from "./src/offline/export.ts";
import type { ExportPreview, ExportRequest } from "./src/offline/export.ts";
import { NativeExportSink } from "./src/offline/nativeExport.ts";
import type { DiagnosticsSaveModule } from "./src/offline/nativeExport.ts";
import { NativeDiagnosticsPicker } from "./src/offline/nativeImport.ts";
import type { DiagnosticsPickerModule } from "./src/offline/nativeImport.ts";
import { AttachmentService } from "./src/offline/attachments.ts";
import { NativeBackgroundSync } from "./src/offline/backgroundNative.ts";
import type {
  BackgroundSyncModuleLike,
  BackgroundSyncStatus,
} from "./src/offline/backgroundNative.ts";
import type {
  DeadLetter,
  LocalAttachment,
  LocalTask,
  OfflineEvent,
  QueueStats,
} from "./src/offline/types.ts";
import { DeviceBroker } from "./src/device/broker.ts";
import { NativeDeviceTransport } from "./src/device/nativeTransport.ts";
import type { NativeDeviceModuleLike } from "./src/device/nativeTransport.ts";
import { formatLocalTime } from "./src/offline/policy.ts";
import { taskViewOf } from "./src/features/taskview/state.ts";
import {
  canExecute,
  projectUserCapabilities,
} from "./src/features/capabilities/status.ts";
import type { UserCapabilityView } from "./src/features/capabilities/status.ts";
import { UpdateManager } from "./src/update/manager.ts";
import { ApiUpdateTransport } from "./src/update/apiTransport.ts";
import { TrustedKeyRing } from "./src/update/trust.ts";
import { releaseKeysForBuild } from "./src/update/devTrust.ts";
import {
  BundleRuntime, sameArtifact,
  BundleRejectedError,
} from "./src/update/bundleRuntime.ts";
import type {
  BundleExecutionRecord, BundleRuntimeState, BundleTool,
} from "./src/update/bundleRuntime.ts";
import {
  FetchArtifactSource,
  NativeArtifactFiles,
} from "./src/update/nativeArtifact.ts";
import {
  loadActiveBundle,
  WorkflowBundleApplier,
} from "./src/update/workflowBundle.ts";
import type { InstalledBundle } from "./src/update/workflowBundle.ts";
import type { UpdateState } from "./src/update/types.ts";
import {
  detectPermissionDialog,
  snapshotTexts,
} from "./src/device/observation.ts";
import type {
  DeviceSnapshot,
  PermissionDialogInfo,
} from "./src/device/observation.ts";
import type {
  ActionJournalEntry,
  DeviceAction,
  DeviceResult,
  DeviceRisk,
} from "./src/device/types.ts";
import { VoiceService } from "./src/device/voice.ts";
import type {
  NativeVoiceModuleLike,
  VoiceCapability,
} from "./src/device/voice.ts";

const POLL_MS = 5000;
const DEFAULT_BASE_URL = "http://10.0.2.2:8300";
const BASE_URL_KEY = "evoagent_base_url";
const APP_VERSION = "0.1.0";

type Section =
  | "tasks"
  | "offline"
  | "workflows"
  | "memory"
  | "settings"
  | "permissions"
  | "updates";

const SECTIONS: { key: Section; label: string }[] = [
  { key: "tasks", label: "任务" },
  { key: "offline", label: "离线状态" },
  { key: "workflows", label: "工作流" },
  { key: "memory", label: "记忆" },
  { key: "settings", label: "预算与模型" },
  { key: "permissions", label: "权限" },
  { key: "updates", label: "更新" },
];

function guessIntent(text: string): string {
  if (/(几点|时间|日期|今天|现在)/.test(text)) return "time_query";
  return "general";
}

function confirmHighRisk(action: DeviceAction, risk: DeviceRisk): Promise<boolean> {
  return new Promise((resolve) => {
    Alert.alert(
      "高风险操作确认",
      `类型: ${action.kind}\n目标: ${action.target}\n风险: ${risk}`,
      [
        { text: "取消", style: "cancel", onPress: () => resolve(false) },
        {
          text: "确认执行",
          style: "destructive",
          onPress: () => resolve(true),
        },
      ],
      { cancelable: false }
    );
  });
}

export default function App(): React.JSX.Element {
  const [goal, setGoal] = useState("");
  const [online, setOnline] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [answerDrafts, setAnswerDrafts] = useState<Record<string, string>>({});
  const [answerBusy, setAnswerBusy] = useState<string | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [localTasks, setLocalTasks] = useState<LocalTask[]>([]);
  const [stats, setStats] = useState<QueueStats | null>(null);
  const [deadLetters, setDeadLetters] = useState<DeadLetter[]>([]);
  const [quarantined, setQuarantined] = useState<OfflineEvent[]>([]);
  const [deviceAvailable, setDeviceAvailable] = useState(false);
  const [deviceResult, setDeviceResult] = useState<DeviceResult | null>(null);
  const [journal, setJournal] = useState<ActionJournalEntry[]>([]);
  const [deviceSnapshot, setDeviceSnapshot] = useState<DeviceSnapshot | null>(
    null
  );
  const [deviceDialog, setDeviceDialog] =
    useState<PermissionDialogInfo | null>(null);
  const [safetyStopped, setSafetyStopped] = useState(false);
  const [clickTarget, setClickTarget] = useState("设置");
  const [system, setSystem] = useState<{
    metrics: MetricsSnapshot;
    candidates: CandidateSummary[];
  } | null>(null);
  const [section, setSection] = useState<Section>("tasks");
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
  const [workflowDetail, setWorkflowDetail] =
    useState<WorkflowCenterDetail | null>(null);
  const [memories, setMemories] = useState<MemoryEntry[]>([]);
  const [memoryDrafts, setMemoryDrafts] = useState<Record<string, string>>({});
  const [models, setModels] = useState<ModelsInfo | null>(null);
  const [releases, setReleases] = useState<ReleasesInfo | null>(null);
  const [releaseVerify, setReleaseVerify] = useState<
    Record<string, ReleaseVerify>
  >({});
  const [versionInfo, setVersionInfo] = useState<VersionInfo | null>(null);
  const [centerBusy, setCenterBusy] = useState(false);
  const [centerError, setCenterError] = useState<string | null>(null);
  const [updateState, setUpdateState] = useState<UpdateState | null>(null);
  const [updateBusy, setUpdateBusy] = useState(false);
  const [activeBundle, setActiveBundle] = useState<InstalledBundle | null>(null);
  const [bundleMessage, setBundleMessage] = useState<string | null>(null);
  const [bundleBusy, setBundleBusy] = useState(false);
  const [runtimeView, setRuntimeView] = useState<BundleRuntimeState | null>(null);
  const [feedbackDrafts, setFeedbackDrafts] = useState<
    Record<string, string>>({});
  const [feedbackMsgs, setFeedbackMsgs] = useState<Record<string, string>>({});
  const [feedbackBusy, setFeedbackBusy] = useState<string | null>(null);
  const [capabilityViews, setCapabilityViews] = useState<
    UserCapabilityView[] | null>(null);
  const [capabilityLoadError, setCapabilityLoadError] = useState<string | null>(null);
  const [capabilityRefresh, setCapabilityRefresh] = useState(0);
  const [exportPreview, setExportPreview] = useState<ExportPreview | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportSensitive, setExportSensitive] = useState(false);
  const [exportMetadataOnly, setExportMetadataOnly] = useState(false);
  const [exportMessage, setExportMessage] = useState<string | null>(null);
  const [adminToken, setAdminToken] = useState("");
  const [importing, setImporting] = useState(false);
  const [importMessage, setImportMessage] = useState<string | null>(null);
  const [localAttachments, setLocalAttachments] = useState<LocalAttachment[]>(
    [],
  );
  const [attachmentBusy, setAttachmentBusy] = useState(false);
  const [attachmentMessage, setAttachmentMessage] = useState<string | null>(
    null,
  );
  const [bgStatus, setBgStatus] = useState<BackgroundSyncStatus | null>(null);
  const [bgBusy, setBgBusy] = useState(false);
  const [bgMessage, setBgMessage] = useState<string | null>(null);
  const [voiceCapability, setVoiceCapability] = useState<VoiceCapability | null>(
    null
  );
  const [voiceBusy, setVoiceBusy] = useState(false);
  const [voiceMessage, setVoiceMessage] = useState<string | null>(null);
  const [speakingTask, setSpeakingTask] = useState<string | null>(null);

  // 服务器地址：模拟器默认 10.0.2.2；真机可填局域网 IP（或 adb reverse 后用 127.0.0.1）
  const [baseUrl, setBaseUrl] = useState(DEFAULT_BASE_URL);
  const [baseUrlDraft, setBaseUrlDraft] = useState(DEFAULT_BASE_URL);

  const services = useMemo(() => {
    const client = api(baseUrl);
    const repo = new OfflineRepository(asyncStorageStore);
    const queue = new EventQueue(repo);
    const transport = new ClientTransport(client);
    const tasks = new LocalTaskService(
      repo,
      transport,
      undefined,
      `client-manifest-${APP_VERSION}`
    );
    const sync = new SyncEngine(queue, transport, tasks);
    const deviceModule =
      (NativeModules as Record<string, unknown>).EvoAgentDevice as
        | (NativeDeviceModuleLike & DiagnosticsSaveModule &
          DiagnosticsPickerModule & BackgroundSyncModuleLike)
        | null;
    const voiceModule = (NativeModules as Record<string, unknown>)
      .EvoAgentVoice as NativeVoiceModuleLike | null | undefined;
    const deviceTransport = new NativeDeviceTransport(deviceModule ?? null);
    const broker = new DeviceBroker(deviceTransport, {
      confirm: confirmHighRisk,
    });
    // 能力包工具注册表：设备动作走既有 DeviceBroker 授权/副作用边界；
    // 纯本地工具只做无副作用计算。未注册工具的节点在执行前被拒绝。
    const bundleTools: Record<string, BundleTool> = {
      local_time: async (args) => ({
        time: formatLocalTime(new Date()),
        ...(typeof args.label === "string" ? { label: args.label } : {}),
      }),
      open_app: async (args) => {
        const result = await broker.execute({
          action_id: `bundle-${Date.now()}`,
          kind: typeof args.kind === "string" ? args.kind : "launch_intent",
          target: typeof args.target === "string" ? args.target : "",
          args,
        });
        if (result.status !== "verified") {
          throw new Error(
            `设备动作未验证(${result.status}): ${result.message ?? "无证据不算成功"}`);
        }
        return result.evidence ?? { status: result.status };
      },
    };
    const bundleRuntime = new BundleRuntime(asyncStorageStore, {
      appVersion: APP_VERSION,
      tools: bundleTools,
      grantedPermissions: () => [],
    });
    const updateFiles = new NativeArtifactFiles();
    const update = new UpdateManager(
      new ApiUpdateTransport(client),
      asyncStorageStore,
      {
        appVersion: APP_VERSION,
        toolContracts: ["open_app"],
        stagingDir: "updates",
        artifactSource: new FetchArtifactSource(),
        artifactFiles: updateFiles,
        trustedKeys: new TrustedKeyRing(releaseKeysForBuild(__DEV__)),
        bundleApplier: new WorkflowBundleApplier(
          updateFiles, asyncStorageStore),
        grantedPermissions: () => [],
        activationAllowed: () => AppState.currentState === "active",
      }
    );
    return {
      client,
      repo,
      queue,
      transport,
      tasks,
      sync,
      deviceTransport,
      broker,
      bundleRuntime,
      update,
      exporter: new DiagnosticsExporter(),
      exportSink: new NativeExportSink(deviceModule ?? null),
      importPicker: new NativeDiagnosticsPicker(deviceModule ?? null),
      attachments: new AttachmentService(repo, client),
      background: new NativeBackgroundSync(deviceModule ?? null),
      voice: new VoiceService(voiceModule ?? null),
    };
  }, [baseUrl]);

  const submitFeedback = useCallback(async (task: LocalTask) => {
    const text = (feedbackDrafts[task.local_task_id] ?? "").trim();
    if (!text || feedbackBusy !== null) return;
    setFeedbackBusy(task.local_task_id);
    try {
      const out = await services.client.postFeedback({
        category: "problem",
        text,
        task_id: task.server_task_id ?? undefined,
        run_id: task.current_run_id ?? undefined,
      });
      setFeedbackMsgs((m) => ({
        ...m,
        [task.local_task_id]: out.aggregated
          ? `已并入同类反馈（第 ${out.count} 条）`
          : task.server_task_id || task.current_run_id
            ? "已提交，将关联此任务的运行证据"
            : "已提交反馈；本地任务尚未关联服务端运行",
      }));
      setFeedbackDrafts((d) => ({ ...d, [task.local_task_id]: "" }));
    } catch (e) {
      setFeedbackMsgs((m) => ({
        ...m,
        [task.local_task_id]:
          e instanceof Error ? e.message : String(e),
      }));
    } finally {
      setFeedbackBusy(null);
    }
  }, [services, feedbackDrafts, feedbackBusy]);

  const reloadLocal = useCallback(async () => {
    setLocalTasks(await services.tasks.list());
    setStats(await services.queue.stats());
    setDeadLetters(await services.queue.deadLetters());
    setQuarantined(await services.queue.quarantined());
    setLocalAttachments(await services.attachments.list());
    setJournal(services.broker.listJournal());
  }, [services]);

  const loadBackgroundSync = useCallback(async () => {
    setBgStatus(await services.background.status());
  }, [services]);

  const loadVoiceStatus = useCallback(async () => {
    setVoiceCapability(await services.voice.status());
  }, [services]);

  const updateBackgroundSync = useCallback(
    async (action: "enable" | "disable" | "run") => {
      setBgBusy(true);
      try {
        if (action !== "disable" && Platform.OS === "android" && Platform.Version >= 33) {
          try {
            await PermissionsAndroid.request(
              PermissionsAndroid.PERMISSIONS.POST_NOTIFICATIONS,
            );
          } catch {
            // 通知权限被拒不影响调度；前台服务通知由系统按策略处理
          }
        }
        const status =
          action === "enable"
            ? await services.background.enable(30, false)
            : action === "disable"
            ? await services.background.disable()
            : await services.background.runNow();
        setBgStatus(status);
        setBgMessage(
          action === "enable"
            ? `已启用：每 ${status.interval_minutes} 分钟，在联网且电量不低时由系统调度。`
            : action === "disable"
            ? "已关闭后台同步；已排队的事件仍保留，联网后可手动同步。"
            : "已请求立即运行一次；系统可能延迟到满足联网条件时执行。",
        );
      } catch (e: unknown) {
        setBgMessage(
          "后台同步操作失败：" + (e instanceof Error ? e.message : String(e)),
        );
      } finally {
        setBgBusy(false);
      }
    },
    [services],
  );

  // 只读系统状态：失败不影响离线队列与在线判定
  const loadSystem = useCallback(async () => {
    try {
      const [metrics, candidates] = await Promise.all([
        services.client.metrics(),
        services.client.candidates(5),
      ]);
      setSystem({ metrics, candidates });
    } catch {
      // 忽略：下次在线同步再刷新
    }
  }, [services]);

  // 用户端中心数据（PRD 89.3-89.8）：仅在线时加载，失败不阻断任务队列
  const loadCenter = useCallback(
    async (name: Section) => {
      if (!online) return;
      if (
        name !== "workflows" &&
        name !== "memory" &&
        name !== "settings" &&
        name !== "updates"
      ) {
        return;
      }
      setCenterBusy(true);
      try {
        if (name === "workflows") {
          setWorkflows(await services.client.workflows());
          setWorkflowDetail(null);
        } else if (name === "memory") {
          setMemories(await services.client.memory());
        } else if (name === "settings") {
          const [modelsInfo, releasesInfo] = await Promise.all([
            services.client.modelsInfo(),
            services.client.releases(),
          ]);
          setModels(modelsInfo);
          setReleases(releasesInfo);
        } else if (name === "updates") {
          const [version, releasesInfo] = await Promise.all([
            services.client.version(),
            services.client.releases(),
          ]);
          setVersionInfo(version);
          setReleases(releasesInfo);
          const checks = await Promise.all(
            releasesInfo.manifests.slice(0, 5).map(async (manifest) => {
              try {
                const detail = await services.client.releaseVerify(
                  manifest.release_id
                );
                return [manifest.release_id, detail.verify] as const;
              } catch {
                return [manifest.release_id, { ok: false }] as const;
              }
            })
          );
          setReleaseVerify(Object.fromEntries(checks));
        }
        setCenterError(null);
      } catch (e: unknown) {
        setCenterError(e instanceof Error ? e.message : String(e));
      } finally {
        setCenterBusy(false);
      }
    },
    [online, services]
  );

  useEffect(() => {
    if (section !== "permissions") return;
    let active = true;
    setCapabilityViews(null);
    setCapabilityLoadError(null);
    void services.client.capabilitiesStatus().then((payload) => {
      if (active) setCapabilityViews(projectUserCapabilities(payload, {
        accessibilityAvailable: deviceAvailable,
      }));
    }).catch((error) => {
      if (active) setCapabilityLoadError(
        error instanceof Error ? error.message : "能力状态加载失败");
    });
    // A response from an old server/device context must not replace the current view.
    return () => { active = false; };
  }, [section, services, deviceAvailable, capabilityRefresh]);

  useEffect(() => {
    void loadCenter(section);
  }, [section, loadCenter]);

  const openWorkflow = useCallback(
    async (workflowId: string) => {
      try {
        setWorkflowDetail(await services.client.workflow(workflowId));
        setCenterError(null);
      } catch (e: unknown) {
        setCenterError(e instanceof Error ? e.message : String(e));
      }
    },
    [services]
  );

  const reloadMemory = useCallback(async () => {
    setMemories(await services.client.memory());
  }, [services]);

  const correctMemory = useCallback(
    async (item: MemoryEntry) => {
      const content = (memoryDrafts[item.memory_id] ?? item.content).trim();
      if (!content) return;
      try {
        await services.client.correctMemory(
          item.memory_id,
          content,
          "客户端纠正"
        );
        setMemoryDrafts((prev) => {
          const next = { ...prev };
          delete next[item.memory_id];
          return next;
        });
        await reloadMemory();
        setCenterError(null);
      } catch (e: unknown) {
        setCenterError(e instanceof Error ? e.message : String(e));
      }
    },
    [memoryDrafts, reloadMemory, services]
  );

  const removeMemory = useCallback(
    (item: MemoryEntry) => {
      Alert.alert("删除记忆", item.content, [
        { text: "取消", style: "cancel" },
        {
          text: "删除",
          style: "destructive",
          onPress: async () => {
            try {
              await services.client.deleteMemory(item.memory_id);
              await reloadMemory();
              setCenterError(null);
            } catch (e: unknown) {
              setCenterError(e instanceof Error ? e.message : String(e));
            }
          },
        },
      ]);
    },
    [reloadMemory, services]
  );

  const flush = useCallback(
    async (force: boolean = false) => {
      setSyncing(true);
      try {
        const result = await services.sync.flush(force);
        if (!result.ok) {
          setOnline(false);
          setError("同步失败：" + (result.error ?? "网络错误"));
        } else if (!result.skipped) {
          setOnline(true);
          setError(null);
          await loadSystem();
        }
        await services.tasks.refresh();
        await reloadLocal();
      } catch (e: unknown) {
        setOnline(false);
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setSyncing(false);
      }
    },
    [services, reloadLocal, loadSystem]
  );

  useEffect(() => {
    (async () => {
      const saved = await asyncStorageStore.getItem(BASE_URL_KEY);
      if (saved) {
        setBaseUrl(saved);
        setBaseUrlDraft(saved);
      }
    })();
  }, []);

  const applyBaseUrl = useCallback(async () => {
    const url = baseUrlDraft.trim().replace(/\/+$/, "");
    if (!/^https?:\/\//.test(url)) {
      setError("服务端地址需以 http:// 或 https:// 开头");
      return;
    }
    await asyncStorageStore.setItem(BASE_URL_KEY, url);
    setBaseUrl(url);
  }, [baseUrlDraft]);

  useEffect(() => {
    let mounted = true;
    (async () => {
      try {
        const state = await services.update.init();
        services.transport.setDeviceInfo({
          installationId: state.installation_id,
          appVersion: APP_VERSION,
        });
        if (mounted) setUpdateState(state);
      } catch {
        // 安装实例初始化失败不阻断离线队列
      }
      try {
        const bundle = await loadActiveBundle(asyncStorageStore);
        if (mounted) setActiveBundle(bundle);
      } catch {
        // 已安装包读取失败不阻断主流程
      }
      try {
        const runtimeState = await services.bundleRuntime.loadState();
        if (mounted) setRuntimeView(runtimeState);
      } catch {
        // 执行记录读取失败不阻断主流程
      }
      await services.tasks.recoverInterrupted();
      const available = await services.deviceTransport.isAvailable();
      if (mounted) setDeviceAvailable(available);
      await reloadLocal();
      await loadBackgroundSync();
      await loadVoiceStatus();
      try {
        await services.client.health();
        if (mounted) setOnline(true);
        await services.sync.flush(false);
      } catch {
        if (mounted) setOnline(false);
      }
      if (mounted) await reloadLocal();
    })();

    const timer = setInterval(async () => {
      if (!mounted) return;
      try {
        await services.client.health();
        if (mounted) setOnline(true);
        await services.sync.flush(false);
        await services.tasks.refresh();
      } catch {
        if (mounted) setOnline(false);
      }
      if (mounted) await reloadLocal();
    }, POLL_MS);

    return () => {
      mounted = false;
      clearInterval(timer);
    };
  }, [services, reloadLocal, loadBackgroundSync, loadVoiceStatus]);

  useEffect(() => {
    if (online) void loadSystem();
  }, [online, loadSystem]);

  const submit = useCallback(async () => {
    const text = goal.trim();
    if (!text) return;
    setBusy(true);
    try {
      const intent = guessIntent(text);
      // 活动能力包声明消费该意图：执行前校验，被拒则不建任务不执行
      if (await services.bundleRuntime.declaresIntent(intent)) {
        try {
          await services.bundleRuntime.resolveExecutable();
        } catch (e: unknown) {
          const detail = e instanceof BundleRejectedError
            ? `(${e.code}) ${e.reasons.join("; ")}`
            : e instanceof Error ? e.message : String(e);
          setError(`能力包执行前拒绝: ${detail}`);
          return;
        }
        const task = await services.tasks.submit(text, intent, "low");
        let record: BundleExecutionRecord;
        try {
          record = await services.bundleRuntime.execute(
            { goal: text }, task.local_task_id);
        } catch (e: unknown) {
          const detail = e instanceof BundleRejectedError
            ? `(${e.code}) ${e.reasons.join("; ")}`
            : e instanceof Error ? e.message : String(e);
          setError(`能力包执行失败: ${detail}`);
          return;
        } finally {
          setRuntimeView(await services.bundleRuntime.loadState());
        }
        setBundleMessage(
          `能力包 ${record.workflow_id}@${record.workflow_version}` +
          `（${record.release_id} · ${record.version}）执行` +
          `${record.outcome === "succeeded" ? "成功" : "失败"}` +
          `，任务 ${task.local_task_id}`);
        setGoal("");
        setError(null);
        await reloadLocal();
        if (online) await flush(true);
        return;
      }
      const task = await services.tasks.submit(text, intent, "low");
      setGoal("");
      setError(null);
      await reloadLocal();
      if (online) {
        await flush(true);
      } else if (task.status === "LOCAL_ONLY") {
        setError("已离线保存；联网后自动以幂等键提交");
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [goal, online, services, reloadLocal, flush]);

  // 语音输入：只在用户点击时录音；识别文本只填入输入框，不自动提交
  const startVoiceInput = useCallback(async () => {
    if (voiceBusy) return;
    setVoiceMessage(null);
    if (Platform.OS === "android") {
      try {
        const granted = await PermissionsAndroid.request(
          PermissionsAndroid.PERMISSIONS.RECORD_AUDIO,
          {
            title: "麦克风权限",
            message:
              "语音输入需要麦克风；识别结果只会填入任务输入框，不会自动提交。",
            buttonPositive: "允许",
            buttonNegative: "取消",
          }
        );
        if (granted !== PermissionsAndroid.RESULTS.GRANTED) {
          setVoiceMessage("未授权麦克风，无法语音输入。");
          return;
        }
      } catch (e: unknown) {
        setVoiceMessage(
          "无法请求麦克风权限：" +
            (e instanceof Error ? e.message : String(e))
        );
        return;
      }
    }
    setVoiceBusy(true);
    try {
      const result = await services.voice.transcribe(15000);
      if (result.status === "transcribed") {
        setGoal(result.transcript);
      }
      setVoiceMessage(result.message);
    } finally {
      setVoiceBusy(false);
      await loadVoiceStatus();
    }
  }, [voiceBusy, services, loadVoiceStatus]);

  const cancelVoiceInput = useCallback(async () => {
    await services.voice.cancel();
    setVoiceBusy(false);
    setVoiceMessage("已取消语音输入");
  }, [services]);

  const speakTask = useCallback(
    async (task: LocalTask) => {
      const text = task.result?.trim();
      if (!text) return;
      setSpeakingTask(task.local_task_id);
      setVoiceMessage(null);
      try {
        const result = await services.voice.speak(text);
        setVoiceMessage(result.message);
        await loadVoiceStatus();
      } finally {
        setSpeakingTask(null);
      }
    },
    [services, loadVoiceStatus]
  );

  const stopSpeaking = useCallback(async () => {
    await services.voice.stop();
    setSpeakingTask(null);
    setVoiceMessage("已停止朗读");
  }, [services]);

  const cancelTask = useCallback(
    async (task: LocalTask) => {
      if (!task.server_task_id) return;
      try {
        await services.client.cancelTask(task.server_task_id);
        await flush(true);
      } catch (e: unknown) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [services, flush]
  );

  const answerTask = useCallback(async (task: LocalTask) => {
    const request = task.waiting_request;
    if (!request || !task.server_task_id) return;
    const answer = answerDrafts[request.request_id]?.trim();
    if (!answer) return;
    setAnswerBusy(request.request_id);
    try {
      await services.client.answerTask(task.server_task_id, request.request_id, answer);
      setAnswerDrafts((drafts) => {
        const next = { ...drafts };
        delete next[request.request_id];
        return next;
      });
      await services.tasks.refresh();
      await reloadLocal();
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setAnswerBusy(null);
    }
  }, [answerDrafts, services, reloadLocal]);

  const confirmReconcile = useCallback(
    async (task: LocalTask) => {
      await services.tasks.confirmResubmit(task.local_task_id);
      await flush(true);
    },
    [services, flush]
  );

  const retryDead = useCallback(
    async (eventId: string) => {
      await services.queue.retryDeadLetter(eventId);
      await reloadLocal();
      await flush(true);
    },
    [services, reloadLocal, flush]
  );

  const retryQuarantined = useCallback(
    async (eventId: string) => {
      await services.queue.retryQuarantined(eventId);
      await reloadLocal();
      await flush(true);
    },
    [services, reloadLocal, flush]
  );

  const runUpdate = useCallback(
    async (op: () => Promise<UpdateState>) => {
      setUpdateBusy(true);
      try {
        setUpdateState(await op());
        setCenterError(null);
        setActiveBundle(await loadActiveBundle(asyncStorageStore));
      } catch (e: unknown) {
        setCenterError(e instanceof Error ? e.message : String(e));
        setUpdateState(await services.update.status());
      } finally {
        setUpdateBusy(false);
      }
    },
    [services]
  );

  /**
   * 有界自动健康探针：真实制品激活后、晋级 last_known_good 之前执行。
   * healthy → 确认健康；unhealthy → 健康检查失败自动回滚并验证原包可执行；
   * unknown → 不标健康也不回滚，等待再次探针。
   */
  const probeActiveBundle = useCallback(async (trigger: string) => {
    const state = await services.update.status();
    if (state.phase !== "active") return;
    const active = state.slots[state.active_slot];
    if (!active?.artifact_path) return; // 非真实制品包保持手工确认
    if (sameArtifact(state.last_known_good, active)) return;
    setBundleBusy(true);
    try {
      const probe = await services.bundleRuntime.runHealthProbe(
        `probe:${trigger}`);
      setRuntimeView(await services.bundleRuntime.loadState());
      setBundleMessage(
        `自动健康探针（${probe.version}）: ${probe.outcome} — ${probe.reason}`);
      const current = await services.update.status();
      if (!sameArtifact(probe, current.slots[current.active_slot])) {
        setBundleMessage("活动版本已变化，本次探针不用于健康晋级或回滚。");
        return;
      }
      if (probe.outcome === "healthy") {
        await runUpdate(() => services.update.healthCheck(true, probe));
      } else if (probe.outcome === "unhealthy") {
        await runUpdate(() => services.update.healthCheck(false, probe));
        const recovery = await services.bundleRuntime.verifyRecovery(
          probe.reason);
        setRuntimeView(await services.bundleRuntime.loadState());
        setActiveBundle(await loadActiveBundle(asyncStorageStore));
        setBundleMessage(
          `新版本 ${probe.version} 健康失败已回滚；` +
          `恢复验证（${recovery.recovered?.version ?? "unknown"}）: ` +
          `${recovery.outcome}`);
      }
      setUpdateState(await services.update.status());
    } catch (e: unknown) {
      setCenterError(
        "健康探针异常：" + (e instanceof Error ? e.message : String(e)));
    } finally {
      setBundleBusy(false);
    }
  }, [services, runUpdate]);

  const activateUpdate = useCallback(() => {
    Alert.alert(
      "激活更新",
      `切换到候选版本 ${updateState?.target?.version ?? ""}？` +
        "上一版本会保留为回滚目标，激活后将自动运行健康探针；" +
        "探针失败会自动回滚并验证原版本可执行。",
      [
        { text: "取消", style: "cancel" },
        {
          text: "激活",
          onPress: () => void (async () => {
            await runUpdate(() => services.update.activate());
            await probeActiveBundle("activate");
          })(),
        },
      ]
    );
  }, [runUpdate, services, probeActiveBundle, updateState]);

  const rollbackUpdate = useCallback(() => {
    Alert.alert("回滚更新", "回滚到最近一次确认健康的版本？", [
      { text: "取消", style: "cancel" },
      {
        text: "回滚",
        style: "destructive",
        onPress: () => void runUpdate(() => services.update.rollback("用户手动回滚")),
      },
    ]);
  }, [runUpdate, services]);

  const refreshSnapshot = useCallback(async () => {
    setDeviceAvailable(await services.deviceTransport.isAvailable());
    const snapshot = await services.deviceTransport.observe();
    setDeviceSnapshot(snapshot);
    setDeviceDialog(snapshot ? detectPermissionDialog(snapshot) : null);
  }, [services]);

  const runDevice = useCallback(
    async (action: DeviceAction) => {
      const result = await services.broker.execute(action);
      setDeviceResult(result);
      setDeviceAvailable(await services.deviceTransport.isAvailable());
      setSafetyStopped(services.broker.isStopped());
      setJournal(services.broker.listJournal());
      await refreshSnapshot();
    },
    [services, refreshSnapshot]
  );

  const resumeDevice = useCallback(() => {
    services.broker.resume();
    setSafetyStopped(false);
  }, [services]);

  const openAccessibilitySettings = useCallback(async () => {
    try {
      if (Platform.OS === "android") {
        await Linking.sendIntent("android.settings.ACCESSIBILITY_SETTINGS");
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const exportRequest = useCallback((): ExportRequest => ({
    device: { app_version: APP_VERSION, platform: Platform.OS },
    include_sensitive_payloads: exportSensitive,
    include_raw_payloads: !exportMetadataOnly,
    include_attachments: false,
  }), [exportSensitive, exportMetadataOnly]);

  const previewExport = useCallback(async () => {
    setExportMessage(null);
    try {
      const state = await services.repo.load();
      setExportPreview(services.exporter.preview(state, exportRequest()));
    } catch (e: unknown) {
      setError("生成预览失败：" + (e instanceof Error ? e.message : String(e)));
    }
  }, [services, exportRequest]);

  const runExport = useCallback(async () => {
    setExporting(true);
    setError(null);
    setExportMessage(null);
    try {
      const state = await services.repo.load();
      const result = await services.exporter.exportTo(
        services.exportSink,
        state,
        exportRequest(),
      );
      setExportPreview(result.preview);
      setExportMessage(
        `已保存 ${result.file_name}（事件 ${result.preview.events} 条）`,
      );
    } catch (e: unknown) {
      setError("导出失败：" + (e instanceof Error ? e.message : String(e)));
    } finally {
      setExporting(false);
    }
  }, [services, exportRequest]);

  const importDiagnostics = useCallback(async () => {
    setImporting(true);
    setError(null);
    setImportMessage(null);
    try {
      const picked = await services.importPicker.pick();
      const result = await services.client.importDiagnosticsPackage(
        picked.fileName,
        picked.bytes,
        adminToken.trim() || undefined,
      );
      const count = result.import?.event_count ?? 0;
      setImportMessage(
        result.duplicate
          ? `该诊断包已导入过（${result.import?.import_id ?? "未知记录"}），未重复入库`
          : `已导入 ${picked.fileName}：事件 ${count} 条，待人工审查 ${
            result.needs_review ?? 0
          } 条`,
      );
    } catch (e: unknown) {
      setError("导入失败：" + (e instanceof Error ? e.message : String(e)));
    } finally {
      setImporting(false);
    }
  }, [services, adminToken]);

  const runAttachmentUpload = useCallback(async () => {
    setAttachmentBusy(true);
    setError(null);
    setAttachmentMessage(null);
    try {
      const picked = await services.importPicker.pick();
      const item = await services.attachments.add(
        picked.fileName,
        "application/octet-stream",
        picked.bytes,
        true,
      );
      await services.attachments.flush();
      const latest = (await services.attachments.list()).find(
        (entry) => entry.attachment_id === item.attachment_id,
      );
      setAttachmentMessage(
        latest?.status === "complete"
          ? `已上传 ${item.file_name}（${item.size_bytes} 字节，SHA-256 校验通过）`
          : `附件已登记但未完成上传：${latest?.last_error ?? "未知原因"}`,
      );
      setLocalAttachments(await services.attachments.list());
    } catch (e: unknown) {
      setError("附件上传失败：" + (e instanceof Error ? e.message : String(e)));
    } finally {
      setAttachmentBusy(false);
    }
  }, [services]);

  const uploadDiagnosticAttachment = useCallback(() => {
    if (!online) {
      setError("离线时无法上传附件，请先恢复服务端连接。");
      return;
    }
    Alert.alert(
      "上传诊断附件",
      "附件可能包含高敏感信息。确认授权后将分块上传，服务端会校验完整 SHA-256；分块进度仅保存在本机，进程重启后需重新选择文件。",
      [
        { text: "取消", style: "cancel" },
        { text: "授权并上传", onPress: () => void runAttachmentUpload() },
      ],
    );
  }, [online, runAttachmentUpload]);

  return (
    <SafeAreaView style={s.container}>
      <KeyboardAvoidingView
        behavior={Platform.OS === "android" ? undefined : "padding"}
        style={s.inner}>
        <ScrollView>
          <Text style={s.title}>EvoAgent 任务中心</Text>
          <View style={[s.banner, online ? s.bannerOnline : s.bannerOffline]}>
            <Text style={s.bannerText}>
              {online ? "在线 · 事件自动同步" : "离线 · 任务保存在本机"}
            </Text>
            <Button
              title={syncing ? "同步中…" : "立即同步"}
              onPress={() => flush(true)}
              disabled={syncing}
            />
          </View>
          <View style={s.serverRow}>
            <TextInput
              style={s.serverInput}
              value={baseUrlDraft}
              onChangeText={setBaseUrlDraft}
              autoCapitalize="none"
              autoCorrect={false}
              placeholder={DEFAULT_BASE_URL}
              onSubmitEditing={() => void applyBaseUrl()}
            />
            <Button title="应用" onPress={() => void applyBaseUrl()} />
          </View>
          <Text style={s.hint}>服务端: {baseUrl}</Text>
          {stats && (
            <Text style={s.hint}>
              待上传 {stats.pending} · 在传 {stats.inFlight} · 隔离{" "}
              {stats.quarantined} · 仅本地 {stats.localOnly} · 死信{" "}
              {stats.dead} · 容量 {stats.usage}/{stats.capacity}（
              {Math.round(stats.usageRatio * 100)}%） · checkpoint{" "}
              {stats.checkpoint}/{stats.capacity}
              {stats.lossMarkers > 0 ? ` · 损失标记 ${stats.lossMarkers}` : ""}
            </Text>
          )}
          {stats?.warning && (
            <Text style={s.error}>
              本地队列接近容量上限：关键事件会优先保留，可重新生成的调试事件可能被清理并记录损失标记，请尽快同步。
            </Text>
          )}
          {error && <Text style={s.error}>{error}</Text>}

          <ScrollView horizontal showsHorizontalScrollIndicator={false}>
            <View style={s.nav}>
              {SECTIONS.map((item) => (
                <View
                  key={item.key}
                  style={[
                    s.navItem,
                    section === item.key ? s.navActive : null,
                  ]}>
                  <Button
                    title={item.label}
                    onPress={() => setSection(item.key)}
                  />
                </View>
              ))}
            </View>
          </ScrollView>

          {section === "offline" && (
            <>
              <Text style={s.section}>离线状态</Text>
              <View style={s.card}>
                <Text style={s.goal}>
                  {online
                    ? "在线：可同步事件并提交服务器任务"
                    : "离线：任务保存在本机，联网后自动同步"}
                </Text>
                <Text style={s.hint}>
                  可用能力：本地任务录入、离线 time_query、设备无障碍操作；
                  受限能力：工作流/记忆/模型设置/更新需要连接服务端。
                </Text>
                {stats && (
                  <Text style={s.hint}>
                    待同步事件 {stats.pending} · 仅本地任务 {stats.localOnly} ·
                    死信 {stats.dead} · 本地回执 {stats.receipts}
                    {stats.lastReceiptAt
                      ? `（最近 ${formatTime(stats.lastReceiptAt)}）`
                      : ""}
                  </Text>
                )}
                {stats && stats.consecutiveFailures > 0 && (
                  <Text style={s.hint}>
                    连续同步失败 {stats.consecutiveFailures} 次
                    {stats.lastSuccessAt
                      ? ` · 上次成功 ${formatTime(stats.lastSuccessAt / 1000)}`
                      : ""}
                  </Text>
                )}
                {stats?.serverUnavailable && (
                  <Text style={s.error}>
                    服务端长时间不可用：事件与任务仍保存在本机，恢复连接后会自动补传；
                    请检查服务端地址或网络。
                  </Text>
                )}
                <Button
                  title={syncing ? "同步中…" : "立即同步"}
                  onPress={() => flush(true)}
                  disabled={syncing || !online}
                />
              </View>

              <Text style={s.section}>后台同步</Text>
              <View style={s.card}>
                <Text style={s.hint}>
                  由系统调度（WorkManager，最短 15 分钟）在联网且电量不低时唤醒无界面
                  JS，复用同一事件队列与幂等提交；不替代前台轮询，也不自动处理待对账任务。
                </Text>
                {bgStatus?.available ? (
                  <Text style={s.hint}>
                    状态：{bgStatus.enabled ? "已启用" : "未启用"} · 间隔{" "}
                    {bgStatus.interval_minutes} 分钟
                    {bgStatus.last_attempt_at
                      ? ` · 最近尝试 ${formatTime(
                          bgStatus.last_attempt_at / 1000,
                        )}`
                      : ""}
                  </Text>
                ) : (
                  <Text style={s.hint}>
                    当前构建未检测到后台同步原生模块，需重新编译安装 APK。
                  </Text>
                )}
                <View style={s.row}>
                  <Button
                    title={bgStatus?.enabled ? "重新调度" : "启用后台同步"}
                    onPress={() => void updateBackgroundSync("enable")}
                    disabled={!bgStatus?.available || bgBusy}
                  />
                  <Button
                    title={bgBusy ? "处理中…" : "立即运行一次"}
                    onPress={() => void updateBackgroundSync("run")}
                    disabled={!bgStatus?.available || bgBusy}
                  />
                </View>
                <Button
                  title="关闭后台同步"
                  onPress={() => void updateBackgroundSync("disable")}
                  disabled={!bgStatus?.available || bgBusy || !bgStatus?.enabled}
                />
                {bgMessage && <Text style={s.hint}>{bgMessage}</Text>}
              </View>

              <Text style={s.section}>诊断包导出</Text>
              <View style={s.card}>
                <Text style={s.hint}>
                  离线生成 ZIP（清单 + 校验和 + 运行时清单），不上传数据，也不改动同步状态；导出前可先预览内容。
                </Text>
                <View style={s.row}>
                  <Button
                    title={
                      exportSensitive
                        ? "敏感事件：包含原文"
                        : "敏感事件：仅元数据"
                    }
                    onPress={() => {
                      setExportSensitive((v) => !v);
                      setExportPreview(null);
                      setExportMessage(null);
                    }}
                  />
                </View>
                <View style={s.row}>
                  <Button
                    title={
                      exportMetadataOnly
                        ? "原始负载：仅字段名"
                        : "原始负载：包含（密钥脱敏）"
                    }
                    onPress={() => {
                      setExportMetadataOnly((v) => !v);
                      setExportPreview(null);
                      setExportMessage(null);
                    }}
                  />
                </View>
                <View style={s.row}>
                  <Button
                    title="预览导出内容"
                    onPress={() => void previewExport()}
                  />
                  <Button
                    title={exporting ? "导出中…" : "导出诊断包"}
                    onPress={() => void runExport()}
                    disabled={exporting}
                  />
                </View>
                {exportPreview && (
                  <Text style={s.hint}>
                    事件 {exportPreview.events} · 敏感{" "}
                    {exportPreview.sensitive_events} · 失败{" "}
                    {exportPreview.failed_events} · 附件 含{" "}
                    {exportPreview.attachments_included}/排除{" "}
                    {exportPreview.attachments_excluded} · 拒绝{" "}
                    {exportPreview.attachments_rejected} · 脱敏字段{" "}
                    {exportPreview.redacted_fields}
                    {exportPreview.loss_markers > 0
                      ? ` · 损失标记 ${exportPreview.loss_markers}`
                      : ""}
                  </Text>
                )}
                {exportPreview?.categories.length ? (
                  <Text style={s.mono}>
                    敏感类别：{exportPreview.categories.join(" / ")}
                  </Text>
                ) : null}
                {exportPreview?.warnings.map((warning) => (
                  <Text key={warning} style={s.error}>
                    {warning}
                  </Text>
                ))}
                {exportMessage && <Text style={s.hint}>{exportMessage}</Text>}
              </View>

              <Text style={s.section}>导入诊断包</Text>
              <View style={s.card}>
                <Text style={s.hint}>
                  从系统文件选择器读取 ZIP 并提交到服务端隔离区（仅证据，不执行）。
                  服务端启用鉴权时需填写 admin 令牌；令牌只随本次请求发送，不落盘。
                </Text>
                <TextInput
                  style={s.serverInput}
                  value={adminToken}
                  onChangeText={setAdminToken}
                  secureTextEntry
                  autoCapitalize="none"
                  autoCorrect={false}
                  placeholder="admin 令牌（未启用鉴权可留空）"
                />
                <Button
                  title={importing ? "导入中…" : "选择诊断包并导入"}
                  onPress={() => void importDiagnostics()}
                  disabled={importing || !online}
                />
                {!online && (
                  <Text style={s.hint}>离线时无法导入，请先恢复服务端连接。</Text>
                )}
                {importMessage && (
                  <Text style={s.hint}>{importMessage}</Text>
                )}
              </View>

              <Text style={s.section}>诊断附件（分块上传）</Text>
              <View style={s.card}>
                <Text style={s.hint}>
                  附件属于高敏感材料，必须显式授权后才会上传；服务端按
                  SHA-256 校验全文，分块进度保存在本机。原始字节仅在本次
                  会话内保留，重启后需重新选择文件。
                </Text>
                <Button
                  title={attachmentBusy ? "上传中…" : "选择附件并授权上传"}
                  onPress={uploadDiagnosticAttachment}
                  disabled={attachmentBusy || !online}
                />
                {attachmentMessage && (
                  <Text style={s.hint}>{attachmentMessage}</Text>
                )}
                {localAttachments.length === 0 ? (
                  <Text style={s.hint}>暂无本地附件记录。</Text>
                ) : (
                  localAttachments.slice(-5).map((item) => (
                    <Text key={item.attachment_id} style={s.mono}>
                      {item.file_name} · {item.size_bytes} 字节 ·{" "}
                      {item.status === "complete"
                        ? "已校验"
                        : item.status === "failed"
                        ? "失败"
                        : item.status === "uploading"
                        ? "上传中"
                        : "待上传"}{" "}
                      · {item.uploaded_chunks.length}/{item.chunk_count} 块
                      {item.last_error ? ` · ${item.last_error}` : ""}
                    </Text>
                  ))
                )}
              </View>
            </>
          )}

          {section === "tasks" && (
            <>
          {online && system && (
            <>
              <Text style={s.section}>系统状态（只读）</Text>
              <View style={s.card}>
                <Text style={s.goal}>
                  任务 {system.metrics.tasks.total} · 成功率{" "}
                  {formatRate(system.metrics.tasks.success_rate)} · 平均{" "}
                  {formatSeconds(system.metrics.tasks.avg_latency_s)}
                </Text>
                <Text style={s.hint}>
                  模型调用 {system.metrics.tasks.model_calls} · 工具调用{" "}
                  {system.metrics.tasks.tool_calls} · 成本{" "}
                  {system.metrics.tasks.cost.toFixed(4)}
                </Text>
                <Text style={s.hint}>
                  诊断案例 {system.metrics.diagnostics.cases} · 发布{" "}
                  {system.metrics.releases.current ?? "-"} · 回滚{" "}
                  {system.metrics.releases.rollbacks}
                </Text>
                <Text style={s.hint}>
                  运行预算 {budgetLine(system.metrics.budget.runtime_day)} · 进化预算{" "}
                  {budgetLine(system.metrics.budget.evolution_day)}
                </Text>
                <Text style={s.mono}>
                  进化候选：
                  {system.candidates.length
                    ? system.candidates
                        .map((c) => `${c.status}(${c.risk_level})`)
                        .join(" · ")
                    : "无"}
                </Text>
              </View>
            </>
          )}

          <View style={s.inputRow}>
            <TextInput
              style={s.input}
              placeholder="输入任务目标，例如：查询现在几点了"
              value={goal}
              onChangeText={setGoal}
            />
            <View style={s.voiceAction}>
              <Button
                title={voiceBusy ? "停止" : "语音"}
                onPress={() =>
                  voiceBusy
                    ? void cancelVoiceInput()
                    : void startVoiceInput()
                }
              />
            </View>
          </View>
          {voiceMessage && <Text style={s.hint}>{voiceMessage}</Text>}
          {busy ? (
            <ActivityIndicator />
          ) : (
            <Button title="提交任务" onPress={submit} disabled={!goal.trim()} />
          )}

          <Text style={s.section}>本地任务</Text>
          {localTasks.length === 0 && <Text style={s.hint}>暂无任务</Text>}
          {localTasks.map((task) => (
            <View key={task.local_task_id} style={s.card}>
              <View style={s.row}>
                <Text style={[s.status, statusColor(task.status)]}>
                  {taskViewOf(task).label}
                </Text>
                {task.status === "NEEDS_RECONCILE" && (
                  <View style={s.action}>
                    <Button
                      title="确认重提"
                      onPress={() => confirmReconcile(task)}
                    />
                  </View>
                )}
                {task.server_task_id &&
                  (task.status === "SUBMITTED" ||
                    task.status === "WAITING_DEPENDENCY") && (
                    <View style={s.action}>
                      <Button
                        title="取消"
                        onPress={() => cancelTask(task)}
                      />
                    </View>
                  )}
              </View>
              <Text style={s.goal}>{task.goal}</Text>
              {taskViewOf(task).wait.reason && (
                <Text style={s.hint}>等待：{taskViewOf(task).wait.reason}</Text>
              )}
              {(task.server_status === "SUCCEEDED" ||
                task.server_status === "FAILED" ||
                task.server_status === "CANCELLED" ||
                task.status === "DONE") && (
                <View style={s.gap}>
                  <TextInput
                    style={s.input}
                    placeholder="反馈问题（自动绑定此任务与运行证据）"
                    value={feedbackDrafts[task.local_task_id] ?? ""}
                    onChangeText={(value) => setFeedbackDrafts((d) => ({
                      ...d, [task.local_task_id]: value,
                    }))}
                    maxLength={4000}
                    multiline
                  />
                  <Button
                    title={feedbackBusy === task.local_task_id
                      ? "提交中…" : "提交反馈"}
                    disabled={feedbackBusy !== null ||
                      !(feedbackDrafts[task.local_task_id] ?? "").trim()}
                    onPress={() => void submitFeedback(task)}
                  />
                  {!!feedbackMsgs[task.local_task_id] && (
                    <Text style={s.hint}>
                      {feedbackMsgs[task.local_task_id]}
                    </Text>
                  )}
                </View>
              )}
              {taskViewOf(task).wait.needsUserAction &&
                !!task.waiting_approvals?.length && task.server_task_id && (
                <ApprovalPanel client={services.client} ids={task.waiting_approvals}
                  taskId={task.server_task_id} online={online} />
              )}
              {task.server_status === "WAITING_USER" && task.waiting_request &&
                !task.waiting_request.answered && (
                <View>
                  <Text style={s.hint}>{task.waiting_request.question}</Text>
                  <TextInput
                    style={s.input}
                    placeholder="补充信息后继续任务"
                    value={answerDrafts[task.waiting_request.request_id] ?? ""}
                    onChangeText={(value) => setAnswerDrafts((drafts) => ({
                      ...drafts, [task.waiting_request!.request_id]: value,
                    }))}
                    maxLength={16000}
                    multiline
                  />
                  <Button title="回答并继续"
                    disabled={answerBusy !== null ||
                      !answerDrafts[task.waiting_request.request_id]?.trim()}
                    onPress={() => void answerTask(task)} />
                </View>
              )}
              {task.result ? (
                <View style={s.resultRow}>
                  <Text style={[s.result, s.resultText]}>
                    结果: {task.result}
                  </Text>
                  <View style={s.action}>
                    <Button
                      title={
                        speakingTask === task.local_task_id ? "停止" : "朗读"
                      }
                      onPress={() =>
                        speakingTask === task.local_task_id
                          ? void stopSpeaking()
                          : void speakTask(task)
                      }
                    />
                  </View>
                </View>
              ) : null}
              {task.failure_reason ? (
                <Text style={s.fail}>失败: {task.failure_reason}</Text>
              ) : null}
              {task.last_sync_error ? (
                <Text style={s.hint}>{task.last_sync_error}</Text>
              ) : null}
              <Text style={s.mono}>幂等键 {task.client_request_id}</Text>
            </View>
          ))}

          {deadLetters.length > 0 && (
            <>
              <Text style={s.section}>死信（需人工处理）</Text>
              {deadLetters.map((letter) => (
                <View key={letter.event.event_id} style={s.card}>
                  <Text style={s.fail}>{letter.reason}</Text>
                  <Text style={s.mono}>
                    {letter.event.event_type} · seq {letter.event.seq}
                  </Text>
                  <Button
                    title="重试"
                    onPress={() => retryDead(letter.event.event_id)}
                  />
                </View>
              ))}
            </>
          )}

          {quarantined.length > 0 && (
            <>
              <Text style={s.section}>隔离事件（服务端拒绝，需人工审查）</Text>
              {quarantined.map((event) => (
                <View key={event.event_id} style={s.card}>
                  <Text style={s.fail}>{event.last_error ?? "服务端拒绝"}</Text>
                  <Text style={s.mono}>
                    {event.event_type} · run {event.run_id ?? "client"} · #
                    {event.sequence_no} · seq {event.seq}
                  </Text>
                  <Button
                    title="重新入队"
                    onPress={() => retryQuarantined(event.event_id)}
                  />
                </View>
              ))}
            </>
          )}

            </>
          )}

          {section === "permissions" && (
            <>
          <Text style={s.section}>权限管理</Text>
          <View style={s.card}>
            <Text style={s.goal}>
              无障碍服务：
              {deviceAvailable ? "已授权" : "未授权"}
            </Text>
            <Text style={s.hint}>
              仅使用节点级操作（读取界面文本、按文本点击、显式 Intent），
              不注入坐标，不绕过系统权限；操作后对比界面节点验证结果。
            </Text>
            <Text style={s.hint}>
              麦克风：
              {voiceCapability === null
                ? "检测中"
                : voiceCapability.permission_granted
                ? "已授权"
                : "未授权"}
              {" · "}语音识别：
              {voiceCapability?.recognition_available ? "可用" : "不可用"}
              {" · "}语音合成：
              {voiceCapability?.tts_available ? "可用" : "不可用"}
            </Text>
            <Text style={s.hint}>
              语音只在点击时使用：识别文本填入任务输入框，不自动提交；
              朗读仅播放任务结果文本。
            </Text>
            <Text style={s.hint}>文件/通知：当前版本未使用</Text>
            <Button title="打开应用权限设置" onPress={() => void Linking.openSettings()} />
          </View>
          <View style={s.card}>
            <Text style={s.goal}>能力状态（六档）</Text>
            <Text style={s.hint}>
              状态来自服务端权威投影：可用 / 待授权 / 已拒绝 / 依赖缺失 /
              未验证 / 未注册；被拒绝的能力不会执行。
            </Text>
            {!!capabilityLoadError && (
              <Text style={s.hint}>能力状态不可用：{capabilityLoadError}</Text>
            )}
            {capabilityViews === null && !capabilityLoadError && (
              <Text style={s.hint}>加载中…</Text>
            )}
            <Button title="刷新能力状态" onPress={() => setCapabilityRefresh((n) => n + 1)} />
            {(capabilityViews ?? []).map((cap) => (
              <View key={cap.tool_id} style={s.gap}>
                <Text style={s.mono}>
                  {cap.tool_id} · {cap.version || "—"}
                </Text>
                <Text style={s.hint}>
                  {cap.state === "ready"
                    ? "✅ 可用"
                    : cap.state === "needs_permission"
                    ? "⏳ 待授权"
                    : cap.state === "blocked"
                    ? "⛔ 不可执行"
                    : cap.state === "needs_setup"
                    ? "🛠 需要设置"
                    : "❔ 未知"}
                  {" · "}
                  {cap.reason}
                </Text>
                {!!cap.recovery && (
                  <Text style={s.hint}>恢复路径：{cap.recovery}</Text>
                )}
              </View>
            ))}
          </View>
          <Text style={s.section}>无障碍设备操作</Text>
          <Text style={s.hint}>
            {deviceAvailable
              ? "服务已启用：仅节点级操作，结果需证据验证"
              : "服务未启用：请在系统设置中授权"}
          </Text>
          <Button
            title="打开系统无障碍设置"
            onPress={openAccessibilitySettings}
          />
          <View style={s.gap} />
          <Button
            title="打开系统设置（低风险）"
            onPress={() =>
              runDevice({
                action_id: `launch-${Date.now()}`,
                kind: "launch_intent",
                target: "com.android.settings",
              })
            }
          />
          <View style={s.inputRow}>
            <TextInput
              style={s.input}
              placeholder="要点击的页面文本"
              value={clickTarget}
              onChangeText={setClickTarget}
            />
          </View>
          <Button
            title="点击页面文本（中风险）"
            onPress={() =>
              runDevice({
                action_id: `click-${Date.now()}`,
                kind: "click_text",
                target: clickTarget.trim(),
              })
            }
            disabled={!clickTarget.trim()}
          />
          <View style={s.row}>
            <Button
              title="快捷：离线状态"
              onPress={() =>
                runDevice({
                  action_id: `click-${Date.now()}`,
                  kind: "click_text",
                  target: "离线状态",
                })
              }
            />
            <Button
              title="快捷：更新"
              onPress={() =>
                runDevice({
                  action_id: `click-${Date.now()}`,
                  kind: "click_text",
                  target: "更新",
                })
              }
            />
          </View>
          <Text style={s.hint}>
            执行状态：{safetyStopped ? "已安全停止（连续未验证）" : "正常"}
          </Text>
          {safetyStopped && (
            <Button title="人工恢复设备操作" onPress={resumeDevice} />
          )}
          <Button
            title="读取当前界面（只读）"
            onPress={() => void refreshSnapshot()}
          />
          {deviceSnapshot && (
            <Text style={s.mono}>
              当前界面 {deviceSnapshot.package_name ?? "未知"} ·{" "}
              {deviceSnapshot.node_count} 节点
              {deviceSnapshot.truncated ? " · 已截断" : ""}
            </Text>
          )}
          {deviceDialog?.detected && (
            <Text style={s.fail}>
              检测到权限弹窗：
              {deviceDialog.buttons.join(" / ") || "需用户处理"}
            </Text>
          )}
          {deviceSnapshot && (
            <Text style={s.hint}>
              {snapshotTexts(deviceSnapshot).slice(0, 8).join(" · ")}
            </Text>
          )}
          {deviceResult && (
            <Text
              style={[
                s.result,
                deviceResult.status === "verified" ? null : s.fail,
              ]}>
              [{deviceResult.status}] {deviceResult.message ?? ""}
            </Text>
          )}
          {journal.slice(-5).reverse().map((entry) => (
            <Text key={entry.action_id} style={s.mono}>
              {entry.kind} · {entry.state} · {entry.risk}
            </Text>
          ))}
            </>
          )}

          {section === "workflows" && (
            <>
              <Text style={s.section}>工作流中心</Text>
              {!online && (
                <Text style={s.hint}>离线：工作流列表需要连接服务端</Text>
              )}
              {centerError && <Text style={s.error}>{centerError}</Text>}
              {centerBusy && <ActivityIndicator />}
              {online && !centerBusy && workflows.length === 0 && (
                <Text style={s.hint}>暂无已发布工作流</Text>
              )}
              {workflows.map((wf) => (
                <View key={`${wf.workflow_id}:${wf.version}`} style={s.card}>
                  <View style={s.row}>
                    <Text style={s.status}>{wf.workflow_id}</Text>
                    <View style={s.action}>
                      <Button
                        title="详情"
                        onPress={() => void openWorkflow(wf.workflow_id)}
                      />
                    </View>
                  </View>
                  <Text style={s.hint}>
                    版本 {wf.version} · {wf.status}
                  </Text>
                  <Text style={s.mono}>更新 {formatTime(wf.updated_at)}</Text>
                </View>
              ))}
              {workflowDetail && (
                <View style={s.card}>
                  <Text style={s.goal}>
                    运行历史（{workflowDetail.runs.length}）
                  </Text>
                  {workflowDetail.versions.map((v) => (
                    <Text key={v.version} style={s.mono}>
                      版本 {v.version} · {v.status}
                    </Text>
                  ))}
                  {workflowDetail.runs.length === 0 && (
                    <Text style={s.hint}>暂无运行记录</Text>
                  )}
                  {workflowDetail.runs.map((run) => (
                    <View key={run.task_id} style={s.run}>
                      <Text style={s.goal}>{run.user_goal}</Text>
                      <Text style={s.hint}>
                        {run.status} · 版本 {run.workflow_version ?? "-"}
                      </Text>
                      <Text style={s.mono}>
                        完成节点 {run.completed_nodes.join(", ") || "-"}
                      </Text>
                      {Object.keys(run.artifacts).length > 0 && (
                        <Text style={s.mono}>
                          产物 {Object.keys(run.artifacts).join(", ")}
                        </Text>
                      )}
                      {run.result ? (
                        <Text style={s.result}>结果: {run.result}</Text>
                      ) : null}
                      {run.failure_reason ? (
                        <Text style={s.fail}>{run.failure_reason}</Text>
                      ) : null}
                    </View>
                  ))}
                </View>
              )}
            </>
          )}

          {section === "memory" && (
            <>
              <Text style={s.section}>记忆中心</Text>
              {!online && (
                <Text style={s.hint}>离线：记忆列表需要连接服务端</Text>
              )}
              {centerError && <Text style={s.error}>{centerError}</Text>}
              {centerBusy && <ActivityIndicator />}
              {online && !centerBusy && memories.length === 0 && (
                <Text style={s.hint}>暂无符合权限范围的记忆</Text>
              )}
              {memories.map((item) => (
                <View key={item.memory_id} style={s.card}>
                  <Text style={s.goal}>{item.content}</Text>
                  <Text style={s.hint}>
                    {item.kind} · {item.scope} · {item.status} · 来源{" "}
                    {item.source}
                  </Text>
                  <Text style={s.mono}>
                    ID {item.memory_id} · 使用 {item.use_count} 次
                  </Text>
                  <TextInput
                    style={[s.input, s.multiline]}
                    value={memoryDrafts[item.memory_id] ?? item.content}
                    onChangeText={(text) =>
                      setMemoryDrafts((prev) => ({
                        ...prev,
                        [item.memory_id]: text,
                      }))
                    }
                    multiline
                  />
                  <View style={s.row}>
                    <View style={s.action}>
                      <Button
                        title="纠正"
                        onPress={() => void correctMemory(item)}
                      />
                    </View>
                    <View style={s.action}>
                      <Button
                        title="删除"
                        color="#cf222e"
                        onPress={() => removeMemory(item)}
                      />
                    </View>
                  </View>
                </View>
              ))}
            </>
          )}

          {section === "settings" && (
            <>
              <Text style={s.section}>预算与模型设置</Text>
              {!online && (
                <Text style={s.hint}>离线：预算与模型信息需要连接服务端</Text>
              )}
              {centerError && <Text style={s.error}>{centerError}</Text>}
              {centerBusy && <ActivityIndicator />}
              {system && (
                <View style={s.card}>
                  <Text style={s.goal}>
                    运行预算 {budgetLine(system.metrics.budget.runtime_day)}
                  </Text>
                  <Text style={s.hint}>
                    进化预算{" "}
                    {budgetLine(system.metrics.budget.evolution_day)}
                  </Text>
                  <Text style={s.hint}>
                    累计模型调用 {system.metrics.tasks.model_calls} 次 · 成本{" "}
                    {system.metrics.tasks.cost.toFixed(4)}
                  </Text>
                </View>
              )}
              {models && (
                <View style={s.card}>
                  <Text style={s.goal}>
                    默认模型 {models.default_model_id || "未配置"}
                  </Text>
                  <Text style={s.hint}>
                    路由条目 {Object.keys(models.routes).length}
                  </Text>
                  {models.models.map((model) => (
                    <View key={model.model_id} style={s.run}>
                      <Text style={s.hint}>
                        {model.model_id} · {model.provider} ·{" "}
                        {model.available ? "可用" : "不可用"}
                      </Text>
                      <Text style={s.mono}>
                        上下文 {model.context_limit} · 质量{" "}
                        {model.quality_score.toFixed(2)} · 延迟{" "}
                        {model.avg_latency_s.toFixed(2)}s · 兼容测试{" "}
                        {model.compat_tested ? "是" : "否"}
                      </Text>
                      <Text style={s.mono}>
                        价格 输入 {model.price_per_1k_input}/1k · 输出{" "}
                        {model.price_per_1k_output}/1k
                      </Text>
                    </View>
                  ))}
                  <Text style={s.hint}>
                    模型选择由服务端路由策略决定，客户端只读展示。
                  </Text>
                </View>
              )}
            </>
          )}

          {section === "updates" && (
            <>
              <Text style={s.section}>更新管理</Text>
              {!online && (
                <Text style={s.hint}>离线：更新信息需要连接服务端</Text>
              )}
              {centerError && <Text style={s.error}>{centerError}</Text>}
              {centerBusy && <ActivityIndicator />}
              <View style={s.card}>
                <Text style={s.goal}>本机版本 {APP_VERSION}</Text>
                <Text style={s.mono}>
                  安装实例 {updateState?.installation_id ?? "初始化中"}
                </Text>
                <Text style={s.hint}>
                  状态 {updateState?.phase ?? "idle"}
                  {updateState?.active_slot
                    ? ` · 活动槽位 ${updateState.active_slot.toUpperCase()}`
                    : ""}
                  {updateState?.last_known_good
                    ? ` · 已知良好 ${updateState.last_known_good.version}`
                    : ""}
                </Text>
                {updateState?.target && (
                  <Text style={s.hint}>
                    候选 {updateState.target.version}（
                    {updateState.target.bundle_kind}）· 灰度{" "}
                    {updateState.target.gray_percent}% · 交付{" "}
                    {updateState.target.delivery_mode}
                  </Text>
                )}
                {updateState?.last_error && (
                  <Text style={s.fail}>{updateState.last_error}</Text>
                )}
                {updateState?.reasons.map((reason, index) => (
                  <Text key={`${index}-${reason}`} style={s.hint}>
                    · {reason}
                  </Text>
                ))}
                <Text style={s.hint}>
                  能力包支持双槽位激活与自动回滚；原生 APK 必须走平台应用更新，
                  客户端只标记为需二进制更新。
                </Text>
                <View style={s.row}>
                  <View style={s.action}>
                    <Button
                      title={updateBusy ? "处理中…" : "检查更新"}
                      onPress={() => void runUpdate(() => services.update.check())}
                      disabled={!online || updateBusy}
                    />
                  </View>
                  <View style={s.action}>
                    <Button
                      title="同步候选状态"
                      onPress={() =>
                        void runUpdate(() => services.update.refreshCandidates())
                      }
                      disabled={!online || updateBusy}
                    />
                  </View>
                  {updateState?.phase === "available" && (
                    <>
                      <View style={s.action}>
                        <Button
                          title="下载并验签"
                          onPress={() =>
                            void runUpdate(() => services.update.prepare())
                          }
                          disabled={updateBusy}
                        />
                      </View>
                      <View style={s.action}>
                        <Button
                          title="校验候选"
                          onPress={() => void runUpdate(() => services.update.verify())}
                          disabled={updateBusy}
                        />
                      </View>
                    </>
                  )}
                </View>
                {updateState?.artifact && (
                  <Text style={s.hint}>
                    已下载验签：{updateState.artifact.version} ·{" "}
                    {updateState.artifact.size} B ·{" "}
                    {updateState.artifact.digest.slice(0, 12)}…
                  </Text>
                )}
                {updateState?.phase === "verified" && (
                  <Button title="激活候选版本" onPress={activateUpdate} />
                )}
                {activeBundle && (
                  <Text style={s.hint}>
                    当前能力包：{activeBundle.bundle.workflow as string} ·
                    {" "}{activeBundle.version} · 槽 {activeBundle.slot}
                    {(activeBundle.bundle as Record<string, unknown>)
                      .workflow_def ? " · 已内嵌可执行 workflow" : ""}
                  </Text>
                )}
                <View style={s.card}>
                  <Text style={s.goal}>能力包执行消费与健康探针</Text>
                  <Text style={s.hint}>
                    活动包声明的意图任务会先经执行前校验并真实执行其 workflow；
                    健康只由执行结果判定，无结果/unknown 不标健康。
                  </Text>
                  {bundleMessage && (
                    <Text style={s.hint}>{bundleMessage}</Text>
                  )}
                  {runtimeView?.binding && (
                    <Text style={s.mono}>
                      最近执行绑定：{runtimeView.binding.workflow_id}
                      @{runtimeView.binding.workflow_version} ·{" "}
                      {runtimeView.binding.release_id} ·{" "}
                      {runtimeView.binding.digest.slice(0, 12)}…
                    </Text>
                  )}
                  {(runtimeView?.probes ?? []).slice(-3).map((probe) => (
                    <View key={probe.probe_id}>
                      <Text style={s.mono}>
                        探针 {probe.version} · {probe.outcome} ·{" "}
                        {formatTime(Math.floor(probe.at / 1000))}
                      </Text>
                      <Text style={s.hint}>
                        {probe.reason}
                        {probe.task_ref ? ` · 关联 ${probe.task_ref}` : ""}
                      </Text>
                    </View>
                  ))}
                  {runtimeView?.recovery && (
                    <Text style={s.hint}>
                      恢复验证：{runtimeView.recovery.outcome} · 失败版本{" "}
                      {runtimeView.recovery.failed?.version ?? "-"} → 恢复版本{" "}
                      {runtimeView.recovery.recovered?.version ?? "-"} ·{" "}
                      {runtimeView.recovery.rollback_reason}
                    </Text>
                  )}
                  <View style={s.row}>
                    <View style={s.action}>
                      <Button
                        title={bundleBusy ? "探针执行中…" : "运行健康探针"}
                        onPress={() => void probeActiveBundle("manual")}
                        disabled={bundleBusy || updateBusy}
                      />
                    </View>
                    <View style={s.action}>
                      <Button
                        title="刷新执行记录"
                        onPress={() =>
                          void services.bundleRuntime.loadState()
                            .then(setRuntimeView)
                        }
                        disabled={bundleBusy || updateBusy}
                      />
                    </View>
                  </View>
                </View>
                {updateState?.phase === "active" && (
                  <>
                    <Text style={s.hint}>激活后请确认功能正常再标记健康。</Text>
                    <View style={s.row}>
                      <View style={s.action}>
                        <Button
                          title="健康检查通过"
                          onPress={() =>
                            void runUpdate(() => services.update.healthCheck(true))
                          }
                          disabled={updateBusy}
                        />
                      </View>
                      <View style={s.action}>
                        <Button
                          title="健康检查失败"
                          color="#cf222e"
                          onPress={() =>
                            void runUpdate(() => services.update.healthCheck(false))
                          }
                          disabled={updateBusy}
                        />
                      </View>
                    </View>
                  </>
                )}
                {Boolean(updateState?.last_known_good) && (
                  <Button
                    title="回滚到已知良好版本"
                    color="#cf222e"
                    onPress={rollbackUpdate}
                    disabled={updateBusy}
                  />
                )}
              </View>
              <View style={s.card}>
                <Text style={s.goal}>候选改进状态</Text>
                {updateState?.candidates_synced_at ? (
                  <Text style={s.hint}>
                    同步于{" "}
                    {formatTime(Math.floor(updateState.candidates_synced_at / 1000))}
                  </Text>
                ) : (
                  <Text style={s.hint}>
                    尚未同步；候选阶段只作透明展示，激活仍走更新对账流程。
                  </Text>
                )}
                {(updateState?.candidates ?? []).map((candidate) => (
                  <View key={candidate.candidate_id}>
                    <Text style={s.mono}>
                      {candidate.candidate_id} · {candidate.status}
                    </Text>
                    <Text style={s.hint}>
                      组件 {candidate.target_component || "-"} · 风险{" "}
                      {candidate.risk_level || "-"}
                      {candidate.release_id
                        ? ` · 版本 ${candidate.release_id}`
                        : ""}
                    </Text>
                  </View>
                ))}
                {updateState?.candidates_synced_at &&
                  (updateState.candidates ?? []).length === 0 && (
                    <Text style={s.hint}>当前没有可展示的候选阶段。</Text>
                  )}
              </View>
              {versionInfo && (
                <View style={s.card}>
                  <Text style={s.goal}>
                    当前版本 {versionInfo.version}
                  </Text>
                  <Text style={s.hint}>
                    发布 {versionInfo.release_id} · 代码{" "}
                    {versionInfo.code_commit}
                  </Text>
                  <Text style={s.hint}>
                    模型适配器 {versionInfo.model_adapter_version} · 工作流{" "}
                    {versionInfo.workflow_versions.join(", ") || "-"}
                  </Text>
                  <Text style={s.mono}>
                    创建 {formatTime(versionInfo.created_at)}
                  </Text>
                </View>
              )}
              {releases?.manifests.map((manifest) => {
                const check = releaseVerify[manifest.release_id];
                return (
                  <View key={manifest.release_id} style={s.card}>
                    <Text style={s.goal}>
                      {manifest.version}
                      {releases.current === manifest.release_id
                        ? "（当前）"
                        : ""}
                    </Text>
                    <Text style={s.hint}>
                      兼容性检查{" "}
                      {check ? (check.ok ? "通过" : "未通过") : "未检查"}
                    </Text>
                    <Text style={s.mono}>
                      回滚目标 {manifest.rollback_target ?? "-"} ·{" "}
                      {formatTime(manifest.created_at)}
                    </Text>
                  </View>
                );
              })}
              {releases && (
                <Text style={s.hint}>
                  部署 {releases.deployments.length} 次 · 回滚{" "}
                  {releases.rollbacks.length} 次；发布说明由管理端维护。
                </Text>
              )}
            </>
          )}
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

function formatRate(rate: number | null): string {
  return rate === null ? "-" : `${Math.round(rate * 100)}%`;
}

function formatSeconds(seconds: number | null): string {
  return seconds === null ? "-" : `${seconds.toFixed(2)}s`;
}

function budgetLine(usage?: Record<string, number>): string {
  if (!usage) return "-";
  return `调用 ${usage.model_calls ?? 0}/成本 ${(usage.cost ?? 0).toFixed(3)}`;
}

function formatTime(seconds: number): string {
  if (!seconds) return "-";
  const d = new Date(seconds * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

function statusColor(status: string) {
  if (status === "DONE") return { color: "#1a7f37" };
  if (status === "NEEDS_RECONCILE") return { color: "#cf222e" };
  if (status === "SUBMITTED" || status === "WAITING_DEPENDENCY") {
    return { color: "#0969da" };
  }
  return { color: "#57606a" };
}

const s = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#f6f8fa" },
  inner: { flex: 1, padding: 16 },
  title: { fontSize: 22, fontWeight: "700", marginBottom: 8 },
  hint: { fontSize: 12, color: "#57606a", marginBottom: 8 },
  serverRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    marginBottom: 4,
  },
  serverInput: {
    flex: 1,
    borderWidth: 1,
    borderColor: "#d0d7de",
    borderRadius: 6,
    paddingHorizontal: 10,
    height: 40,
    backgroundColor: "#fff",
  },
  error: { color: "#cf222e", marginBottom: 8 },
  banner: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    borderRadius: 8,
    paddingHorizontal: 12,
    paddingVertical: 6,
    marginBottom: 8,
  },
  bannerOnline: { backgroundColor: "#dafbe1" },
  bannerOffline: { backgroundColor: "#ffebe9" },
  bannerText: { fontWeight: "600" },
  inputRow: { flexDirection: "row", marginBottom: 8, marginTop: 8 },
  voiceAction: { width: 88, marginLeft: 8, justifyContent: "center" },
  resultRow: { flexDirection: "row", alignItems: "center", gap: 8 },
  resultText: { flex: 1 },
  input: {
    flex: 1,
    borderWidth: 1,
    borderColor: "#d0d7de",
    borderRadius: 6,
    paddingHorizontal: 10,
    height: 44,
    backgroundColor: "#fff",
  },
  section: { fontSize: 17, fontWeight: "700", marginTop: 18, marginBottom: 8 },
  card: {
    backgroundColor: "#fff",
    borderRadius: 8,
    padding: 12,
    marginBottom: 8,
    borderWidth: 1,
    borderColor: "#d0d7de",
  },
  row: { flexDirection: "row", justifyContent: "space-between" },
  status: { fontWeight: "700" },
  action: { width: 100 },
  goal: { marginTop: 4, fontSize: 15 },
  result: { marginTop: 4, color: "#1a7f37" },
  fail: { marginTop: 4, color: "#cf222e" },
  mono: { marginTop: 4, fontSize: 11, color: "#57606a" },
  nav: { flexDirection: "row", gap: 8, marginBottom: 8 },
  navItem: {
    borderRadius: 6,
    borderWidth: 1,
    borderColor: "#d0d7de",
    backgroundColor: "#fff",
    paddingHorizontal: 4,
  },
  navActive: { backgroundColor: "#ddf4ff", borderColor: "#0969da" },
  run: {
    marginTop: 8,
    paddingTop: 8,
    borderTopWidth: 1,
    borderTopColor: "#eaeef2",
  },
  multiline: { height: 72, marginTop: 8, textAlignVertical: "top" },
  gap: { height: 8 },
});
