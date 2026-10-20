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
import type { CandidateSummary, MetricsSnapshot } from "./src/api.ts";
import { asyncStorageStore } from "./src/storage/asyncStorage.ts";
import { OfflineRepository } from "./src/offline/storage.ts";
import { EventQueue } from "./src/offline/queue.ts";
import { LocalTaskService } from "./src/offline/tasks.ts";
import { ClientTransport } from "./src/offline/apiTransport.ts";
import { SyncEngine } from "./src/offline/sync.ts";
import type { DeadLetter, LocalTask, QueueStats } from "./src/offline/types.ts";
import { DeviceBroker } from "./src/device/broker.ts";
import { NativeDeviceTransport } from "./src/device/nativeTransport.ts";
import type { NativeDeviceModuleLike } from "./src/device/nativeTransport.ts";
import type {
  ActionJournalEntry,
  DeviceAction,
  DeviceResult,
  DeviceRisk,
} from "./src/device/types.ts";

const POLL_MS = 5000;
const DEFAULT_BASE_URL = "http://10.0.2.2:8300";
const BASE_URL_KEY = "evoagent_base_url";

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
  const [deviceAvailable, setDeviceAvailable] = useState(false);
  const [deviceResult, setDeviceResult] = useState<DeviceResult | null>(null);
  const [journal, setJournal] = useState<ActionJournalEntry[]>([]);
  const [clickTarget, setClickTarget] = useState("设置");
  const [system, setSystem] = useState<{
    metrics: MetricsSnapshot;
    candidates: CandidateSummary[];
  } | null>(null);

  // 服务器地址：模拟器默认 10.0.2.2；真机可填局域网 IP（或 adb reverse 后用 127.0.0.1）
  const [baseUrl, setBaseUrl] = useState(DEFAULT_BASE_URL);
  const [baseUrlDraft, setBaseUrlDraft] = useState(DEFAULT_BASE_URL);

  const services = useMemo(() => {
    const client = api(baseUrl);
    const repo = new OfflineRepository(asyncStorageStore);
    const queue = new EventQueue(repo);
    const transport = new ClientTransport(client);
    const tasks = new LocalTaskService(repo, transport);
    const sync = new SyncEngine(queue, transport, tasks);
    const deviceModule =
      (NativeModules as Record<string, unknown>).EvoAgentDevice as
        | NativeDeviceModuleLike
        | null;
    const deviceTransport = new NativeDeviceTransport(deviceModule ?? null);
    const broker = new DeviceBroker(deviceTransport, {
      confirm: confirmHighRisk,
    });
    return { client, repo, queue, tasks, sync, deviceTransport, broker };
  }, [baseUrl]);

  const reloadLocal = useCallback(async () => {
    setLocalTasks(await services.tasks.list());
    setStats(await services.queue.stats());
    setDeadLetters(await services.queue.deadLetters());
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

  const runDevice = useCallback(
    async (action: DeviceAction) => {
      const result = await services.broker.execute(action);
      setDeviceResult(result);
      setDeviceAvailable(await services.deviceTransport.isAvailable());
      setJournal(services.broker.listJournal());
    },
    [services]
  );

  const openAccessibilitySettings = useCallback(async () => {
    try {
      if (Platform.OS === "android") {
        await Linking.sendIntent("android.settings.ACCESSIBILITY_SETTINGS");
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

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
              待上传 {stats.pending} · 仅本地 {stats.localOnly} · 死信{" "}
              {stats.dead} · checkpoint {stats.checkpoint}/{stats.capacity}
            </Text>
          )}
          {error && <Text style={s.error}>{error}</Text>}

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
  gap: { height: 8 },
});
