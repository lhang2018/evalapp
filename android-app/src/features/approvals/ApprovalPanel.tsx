import React, { useEffect, useMemo, useState } from "react";
import { Button, StyleSheet, Text, TextInput, View } from "react-native";
import type { ApprovalTransport, ApprovalState } from "./state.ts";
import { ApprovalSession, approvalStatus, fullBinding } from "./state.ts";

function ApprovalRow({ client, id, taskId, token, online }: {
  client: ApprovalTransport; id: string; taskId: string; token: string; online: boolean;
}) {
  const [state, setState] = useState<ApprovalState>({ busy: false, verified: false, error: "", notice: "" });
  const [, tick] = useState(0);
  const session = useMemo(() => new ApprovalSession(client, id, taskId, setState), [client, id, taskId]);
  useEffect(() => {
    session.configure(token, online);
    void session.refresh();
    const poll = setInterval(() => { tick(v => v + 1); void session.refresh(); }, 5000);
    return () => clearInterval(poll);
  }, [session, token, online]);
  useEffect(() => () => session.dispose(), [session]);
  const detail = state.detail;
  return <View style={s.row}>
    <Text selectable>审批 {id} · {approvalStatus(detail)}</Text>
    {detail && <>
      <Text selectable>{detail.action_summary}</Text>
      <Text selectable>任务：{detail.task_id} · 操作：{detail.op}</Text>
      <Text selectable>有效期：{detail.expires_at ? new Date(detail.expires_at * 1000).toLocaleString() : "未提供"}</Text>
      <Text selectable>动作摘要校验值：{detail.action_digest ?? "未提供"}</Text>
      <Text style={s.binding} selectable>{fullBinding(detail)}</Text>
    </>}
    {!!state.error && <Text accessibilityRole="alert">{state.error}</Text>}
    {!!state.notice && <Text>{state.notice}</Text>}
    {!!state.taskStatus && <Text>任务状态：{state.taskStatus}</Text>}
    {!state.verified && <Text>详情待本次会话刷新核验，审批按钮已停用。</Text>}
    {!online && <Text>离线：可查看已读取详情，联网后刷新审批。</Text>}
    <Button title={state.busy ? "正在请求…" : "刷新审批状态"}
      disabled={state.busy || !online || !token.trim()} onPress={() => void session.refresh()} />
    <Button title="批准此动作" disabled={!session.canDecide()} onPress={() => void session.decide(true)} />
    <Button title="拒绝并取消任务" disabled={!session.canDecide()} onPress={() => void session.decide(false)} />
  </View>;
}

export function ApprovalPanel({ client, ids, taskId, online }: {
  client: ApprovalTransport; ids: string[]; taskId: string; online: boolean;
}) {
  const [token, setToken] = useState("");
  return <View>
    <Text>管理员审批 · 请核对完整动作与参数</Text>
    <TextInput style={s.input} value={token} onChangeText={setToken} secureTextEntry
      autoCapitalize="none" autoCorrect={false} placeholder="管理员凭证（仅本次页面使用）"
      accessibilityLabel="管理员审批凭证" />
    <Button title="清除凭证" onPress={() => setToken("")} disabled={!token} />
    {ids.map(id => <ApprovalRow key={id} client={client} id={id} taskId={taskId} token={token} online={online} />)}
  </View>;
}
const s = StyleSheet.create({
  row: { padding: 10, marginVertical: 6, backgroundColor: "#eef2f7", gap: 6 },
  input: { borderWidth: 1, borderColor: "#999", padding: 8, marginVertical: 6 },
  binding: { fontFamily: "monospace", fontSize: 12 },
});
