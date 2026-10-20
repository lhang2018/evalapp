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
  Button,
  KeyboardAvoidingView,
  Linking,
  NativeModules,
  Platform,
  SafeAreaView,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { api } from "./src/api.ts";
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
import type {
  DeadLetter,
  LocalTask,
  OfflineEvent,
  QueueStats,
} from "./src/offline/types.ts";
import { DeviceBroker } from "./src/device/broker.ts";
import { NativeDeviceTransport } from "./src/device/nativeTransport.ts";
import type { NativeDeviceModuleLike } from "./src/device/nativeTransport.ts";
import { UpdateManager } from "./src/update/manager.ts";
import { ApiUpdateTransport } from "./src/update/apiTransport.ts";
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
  const [exportPreview, setExportPreview] = useState<ExportPreview | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportSensitive, setExportSensitive] = useState(false);
  const [exportMetadataOnly, setExportMetadataOnly] = useState(false);
  const [exportMessage, setExportMessage] = useState<string | null>(null);

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
        | (NativeDeviceModuleLike & DiagnosticsSaveModule)
        | null;
    const deviceTransport = new NativeDeviceTransport(deviceModule ?? null);
    const broker = new DeviceBroker(deviceTransport, {
      confirm: confirmHighRisk,
    });
    const update = new UpdateManager(
      new ApiUpdateTransport(client),
      asyncStorageStore,
      { appVersion: APP_VERSION }
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
      update,
      exporter: new DiagnosticsExporter(),
      exportSink: new NativeExportSink(deviceModule ?? null),
    };
  }, [baseUrl]);

  const reloadLocal = useCallback(async () => {
    setLocalTasks(await services.tasks.list());
    setStats(await services.queue.stats());
    setDeadLetters(await services.queue.deadLetters());
    setQuarantined(await services.queue.quarantined());
    setJournal(services.broker.listJournal());
  }, [services]);

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
      await services.tasks.recoverInterrupted();
      const available = await services.deviceTransport.isAvailable();
      if (mounted) setDeviceAvailable(available);
      await reloadLocal();
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
  }, [services, reloadLocal]);

  useEffect(() => {
    if (online) void loadSystem();
  }, [online, loadSystem]);

  const submit = useCallback(async () => {
    const text = goal.trim();
    if (!text) return;
    setBusy(true);
    try {
      const task = await services.tasks.submit(text, guessIntent(text), "low");
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
      } catch (e: unknown) {
        setCenterError(e instanceof Error ? e.message : String(e));
        setUpdateState(await services.update.status());
      } finally {
        setUpdateBusy(false);
      }
    },
    [services]
  );

  const activateUpdate = useCallback(() => {
    Alert.alert(
      "激活更新",
      `切换到候选版本 ${updateState?.target?.version ?? ""}？` +
        "上一版本会保留为回滚目标，健康检查失败将自动回滚。",
      [
        { text: "取消", style: "cancel" },
        {
          text: "激活",
          onPress: () => void runUpdate(() => services.update.activate()),
        },
      ]
    );
  }, [runUpdate, services, updateState]);

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
                    死信 {stats.dead}
                  </Text>
                )}
                <Button
                  title={syncing ? "同步中…" : "立即同步"}
                  onPress={() => flush(true)}
                  disabled={syncing || !online}
                />
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
          </View>
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
                  {task.status}
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
              {task.result ? (
                <Text style={s.result}>结果: {task.result}</Text>
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
            <Text style={s.hint}>麦克风：未申请（语音入口待迭代）</Text>
            <Text style={s.hint}>文件/通知：当前版本未使用</Text>
            <Button title="打开应用权限设置" onPress={() => void Linking.openSettings()} />
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
                    <View style={s.action}>
                      <Button
                        title="校验候选"
                        onPress={() => void runUpdate(() => services.update.verify())}
                        disabled={updateBusy}
                      />
                    </View>
                  )}
                </View>
                {updateState?.phase === "verified" && (
                  <Button title="激活候选版本" onPress={activateUpdate} />
                )}
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
