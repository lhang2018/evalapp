/**
 * EvoAgent Android 客户端主界面（PRD 89 用户端功能）。
 *
 - 任务首页：文本输入提交任务（89.1）
 - 任务详情：执行阶段/结果/失败原因/预算（89.2）
 - 任务列表轮询（89.2 / 89.5）
 *
 * UI 可通过 OTA（CodePush / Expo Updates）整包推送更新 —— JS 层不依赖原生改动。
 */
import React, { useCallback, useEffect, useState } from "react";
import {
  ActivityIndicator,
  Button,
  FlatList,
  KeyboardAvoidingView,
  Platform,
  SafeAreaView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { api, TaskInfo } from "./src/api";

const AUTO_POLL_MS = 1500;

export default function App(): React.JSX.Element {
  const [goal, setGoal] = useState("");
  const [tasks, setTasks] = useState<TaskInfo[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 服务器地址：开发用本机；真机用局域网 IP（在设置页可改，此处先简单常量）
  const baseUrl = "http://10.0.2.2:8300";

  const refresh = useCallback(async () => {
    try {
      setTasks(await api(baseUrl).listTasks());
      setError(null);
    } catch (e: any) {
      setError("无法连接 EvoAgent 服务端：" + e.message);
    }
  }, [baseUrl]);

  useEffect(() => {
    const timer = setInterval(refresh, AUTO_POLL_MS);
    refresh();
    return () => clearInterval(timer);
  }, [refresh]);

  const submit = useCallback(async () => {
    if (!goal.trim()) return;
    setBusy(true);
    try {
      await api(baseUrl).submitTask(goal.trim(), "general");
      setGoal("");
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }, [baseUrl, goal, refresh]);

  const cancel = useCallback(
    async (id: string) => {
      try {
        await api(baseUrl).cancelTask(id);
        await refresh();
      } catch (e: any) {
        setError(e.message);
      }
    },
    [baseUrl, refresh]
  );

  return (
    <SafeAreaView style={s.container}>
      <KeyboardAvoidingView
        behavior={Platform.OS === "android" ? undefined : "padding"}
        style={s.inner}>
        <Text style={s.title}>EvoAgent 任务中心</Text>
        <Text style={s.hint}>服务端: {baseUrl}（/health 健康状态见上方提示）</Text>
        {error && <Text style={s.error}>{error}</Text>}
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
        <FlatList
          style={s.list}
          data={tasks}
          keyExtractor={(t) => t.task_id}
          renderItem={({ item }) => (
            <TaskCard task={item} onCancel={() => cancel(item.task_id)} />
          )}
        />
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

function TaskCard({ task, onCancel }: { task: TaskInfo; onCancel: () => void }) {
  const done =
    task.task_status === "SUCCEEDED" || task.task_status === "FAILED" ||
    task.task_status === "CANCELLED";
  const open = !done;
  return (
    <View style={s.card}>
      <View style={s.row}>
        <Text style={[s.status, statusColor(task.task_status)]}>
          {task.task_status}
        </Text>
        {open && (
          <View style={s.cancel}>
            <Button title="取消" onPress={onCancel} />
          </View>
        )}
      </View>
      <Text style={s.goal}>{task.user_goal}</Text>
      {task.result ? <Text style={s.result}>结果: {task.result}</Text> : null}
      {task.failure_reason ? (
        <Text style={s.fail}>失败: {task.failure_reason}</Text>
      ) : null}
    </View>
  );
}

function statusColor(status: string) {
  if (status === "SUCCEEDED") return { color: "#1a7f37" };
  if (status === "FAILED") return { color: "#cf222e" };
  if (status === "RUNNING" || status === "VERIFYING") return { color: "#0969da" };
  return { color: "#57606a" };
}

const s = StyleSheet.create({
  container: { flex: 1, backgroundColor: "#f6f8fa" },
  inner: { flex: 1, padding: 16 },
  title: { fontSize: 22, fontWeight: "700", marginBottom: 8 },
  hint: { fontSize: 12, color: "#57606a", marginBottom: 8 },
  error: { color: "#cf222e", marginBottom: 8 },
  inputRow: { flexDirection: "row", marginBottom: 8 },
  input: {
    flex: 1, borderWidth: 1, borderColor: "#d0d7de",
    borderRadius: 6, paddingHorizontal: 10, height: 44, backgroundColor: "#fff",
  },
  list: { marginTop: 12 },
  card: {
    backgroundColor: "#fff", borderRadius: 8, padding: 12,
    marginBottom: 8, borderWidth: 1, borderColor: "#d0d7de",
  },
  row: { flexDirection: "row", justifyContent: "space-between" },
  status: { fontWeight: "700" },
  cancel: { width: 90 },
  goal: { marginTop: 4, fontSize: 15 },
  result: { marginTop: 4, color: "#1a7f37" },
  fail: { marginTop: 4, color: "#cf222e" },
});
