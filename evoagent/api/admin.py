"""Web 管理端（PRD 90.1-90.7）。

分区：运行健康、问题中心与诊断详情、候选实验中心、版本中心、成本中心、
审计与回滚中心，另有任务回放、备份恢复与候选代码隔离执行。
所有动态内容用 textContent / createElement 写入，避免把服务端数据当 HTML 注入。
"""

ADMIN_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EvoAgent 管理端</title>
<style>
  :root { color-scheme: light dark; --line:#8884; --accent:#2d7ff9; }
  body { font: 14px/1.5 system-ui, "Segoe UI", sans-serif; margin: 0; }
  header { padding: 12px 20px; border-bottom: 1px solid var(--line);
           display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  h1 { font-size: 17px; margin: 0; }
  nav { padding: 8px 20px; border-bottom: 1px solid var(--line);
        display: flex; gap: 6px; flex-wrap: wrap; }
  nav button.active { background: var(--accent); border-color: var(--accent);
                      color: #fff; }
  main { padding: 16px 20px; }
  .panel { display: none; }
  .panel.active { display: block; }
  section { border: 1px solid var(--line); border-radius: 8px; padding: 12px;
            margin-bottom: 16px; }
  h2 { font-size: 14px; margin: 0 0 8px; }
  h3 { font-size: 13px; margin: 12px 0 6px; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { text-align: left; padding: 4px 6px; border-bottom: 1px solid var(--line);
           vertical-align: top; }
  td.actions { white-space: nowrap; }
  code, pre { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
  pre { white-space: pre-wrap; word-break: break-word; max-height: 360px;
        overflow: auto; }
  input, select, button, textarea { font: inherit; padding: 4px 8px; }
  textarea { width: 100%; min-height: 110px; font-family: ui-monospace, Consolas, monospace; }
  .muted { opacity: .7; }
  .err { color: #d33; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
         margin-bottom: 8px; }
  .grid2 { display: grid; gap: 12px;
           grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); }
</style>
</head>
<body>
<header>
  <h1>EvoAgent 管理端</h1>
  <span class="muted" id="stamp"></span>
  <input id="token" type="password" placeholder="API Token（如已启用鉴权）" size="28">
  <button id="refresh">刷新当前面板</button>
  <span id="status" class="muted"></span>
</header>
<nav>
  <button data-tab="health">运行健康</button>
  <button data-tab="problems">问题中心</button>
  <button data-tab="case">诊断详情</button>
  <button data-tab="candidates">候选实验中心</button>
  <button data-tab="versions">版本中心</button>
  <button data-tab="costs">成本中心</button>
  <button data-tab="audit">审计与回滚</button>
  <button data-tab="replay">任务回放</button>
  <button data-tab="backup">备份与恢复</button>
  <button data-tab="sandbox">隔离沙箱</button>
</nav>
<main>

<div class="panel" id="panel-health">
  <div class="grid2">
    <section><h2>健康</h2><pre id="health">-</pre></section>
    <section><h2>任务指标</h2><div id="healthTasks">-</div>
      <h3>预算用量</h3><div id="healthBudget">-</div></section>
  </div>
  <div class="grid2">
    <section><h2>分级告警</h2><div id="healthAlerts">-</div></section>
    <section><h2>工具健康</h2><div id="healthTools">-</div></section>
  </div>
  <section><h2>最近任务</h2><div id="healthRecent">-</div></section>
</div>

<div class="panel" id="panel-problems">
  <section>
    <h2>问题中心</h2>
    <div class="row">
      <button id="diagScan">扫描失败任务</button>
      <span id="diagScanOut" class="muted"></span>
    </div>
    <div id="diagCases">-</div>
  </section>
  <div class="grid2">
    <section><h2>诊断趋势</h2><pre id="diagTrends">-</pre></section>
    <section><h2>环境漂移</h2>
      <div class="row"><button id="driftScan">生成漂移报告</button></div>
      <pre id="diagDrift">-</pre>
    </section>
  </div>
</div>

<div class="panel" id="panel-case">
  <section>
    <h2>诊断详情（假设 / 证据 / 不确定性）</h2>
    <div class="row">
      <input id="caseId" placeholder="case_id" size="24">
      <button id="caseLoad">加载</button>
      <button id="caseSuggest">模型辅助假设</button>
      <span class="muted">模型解释仅作辅助，不改变案例状态</span>
    </div>
    <div id="caseInfo">-</div>
    <h3>证据快照</h3><pre id="caseEvidence">-</pre>
    <h3>假设</h3>
    <div class="row">
      <label>探针
        <select id="probeSelect">
          <option value="tool_health">tool_health</option>
          <option value="calc_roundtrip">calc_roundtrip</option>
          <option value="file_write_sandbox">file_write_sandbox</option>
          <option value="state_machine_selfcheck">state_machine_selfcheck</option>
          <option value="memory_roundtrip">memory_roundtrip</option>
          <option value="context_constraint_retention">context_constraint_retention</option>
        </select>
      </label>
      <label>故障注入
        <select id="faultSelect">
          <option value="">无</option>
          <option value="error">error</option>
          <option value="timeout">timeout</option>
          <option value="fail">fail</option>
        </select>
      </label>
    </div>
    <div id="caseHypotheses">-</div>
    <h3>探针实验</h3>
    <div id="caseExperiments">-</div>
    <pre id="caseOut">-</pre>
  </section>
</div>

<div class="panel" id="panel-candidates">
  <section>
    <h2>候选实验中心</h2>
    <div class="row">
      <label>状态
        <select id="candStatus">
          <option value="">全部</option>
          <option value="DETECTED">DETECTED</option>
          <option value="TRIAGED">TRIAGED</option>
          <option value="HYPOTHESIZED">HYPOTHESIZED</option>
          <option value="CANDIDATE_CREATED">CANDIDATE_CREATED</option>
          <option value="TESTING">TESTING</option>
          <option value="EVALUATING">EVALUATING</option>
          <option value="REJECTED">REJECTED</option>
          <option value="APPROVAL_PENDING">APPROVAL_PENDING</option>
          <option value="RELEASE_READY">RELEASE_READY</option>
          <option value="MONITORING">MONITORING</option>
          <option value="PROMOTED">PROMOTED</option>
          <option value="ROLLED_BACK">ROLLED_BACK</option>
        </select>
      </label>
    </div>
    <div id="candTable">-</div>
    <h3>候选详情 / 动作输出</h3>
    <pre id="candOut">-</pre>
  </section>
</div>

<div class="panel" id="panel-versions">
  <section>
    <h2>版本中心 · 发布</h2>
    <pre id="versionSummary">-</pre>
    <div id="versionReleases">-</div>
  </section>
  <section>
    <h2>版本中心 · 模型与迁移</h2>
    <div class="row">
      <input id="migOld" placeholder="old_model_id" size="18">
      <input id="migNew" placeholder="new_model_id" size="18">
      <input id="migDataset" placeholder="dataset_id（可选）" size="20">
      <button id="migCreate">创建迁移</button>
    </div>
    <div id="versionModels">-</div>
    <h3>迁移执行链</h3>
    <div id="versionMigrations">-</div>
    <pre id="versionOut">-</pre>
  </section>
  <section>
    <h2>版本中心 · 工作流</h2>
    <div id="versionWorkflows">-</div>
    <h3>抽取候选</h3>
    <div class="row"><button id="wfExtract">抽取成功模式</button></div>
    <div id="versionWfCands">-</div>
  </section>
</div>

<div class="panel" id="panel-costs">
  <section>
    <h2>成本中心</h2>
    <div id="costTotals">-</div>
    <div class="grid2">
      <div><h3>按领域</h3><div id="costDomain">-</div></div>
      <div><h3>按模型</h3><div id="costModel">-</div></div>
      <div><h3>按版本</h3><div id="costVersion">-</div></div>
    </div>
    <h3>进化实验成本</h3><pre id="costEvolution">-</pre>
    <h3>按任务（最近 50）</h3><div id="costTasks">-</div>
  </section>
</div>

<div class="panel" id="panel-audit">
  <section>
    <h2>审计与回滚中心</h2>
    <div class="row">
      <span>当前发布：<b id="auditCurrent">-</b></span>
      <input id="rollbackReason" placeholder="回滚原因" size="30">
      <button id="releaseRollback">回滚当前发布</button>
    </div>
    <pre id="auditOut">-</pre>
    <h3>审计日志</h3>
    <div id="auditTable">-</div>
    <h3>日志分层与保留期</h3>
    <div id="auditLayers">-</div>
  </section>
</div>

<div class="panel" id="panel-replay">
  <section>
    <h2>任务回放（只读）</h2>
    <div class="row">
      <input id="replayTask" placeholder="task_id" size="24">
      <button id="replayBtn">回放</button>
    </div>
    <pre id="replayOut">-</pre>
  </section>
</div>

<div class="panel" id="panel-backup">
  <section>
    <h2>备份与恢复</h2>
    <div class="row">
      <button id="backupCreate">创建备份</button>
      <input id="restoreName" placeholder="备份文件名" size="30">
      <button id="restoreBtn">恢复</button>
    </div>
    <div id="backups"></div>
    <pre id="backupOut">-</pre>
  </section>
</div>

<div class="panel" id="panel-sandbox">
  <section>
    <h2>候选代码隔离执行（admin）</h2>
    <p class="muted">一次性目录；网络与子进程被审计钩子阻断；凭据不继承；超时强杀。
      该环境降低风险，但不是对恶意代码的强安全边界。</p>
    <textarea id="sandboxCode" placeholder="print('hello')"></textarea>
    <div class="row">
      <label>超时（秒）<input id="sandboxTimeout" type="number" value="5" min="1" max="30" style="width:70px"></label>
      <button id="sandboxRun">执行</button>
    </div>
    <pre id="sandboxOut">-</pre>
  </section>
</div>

</main>
<script>
const $ = (id) => document.getElementById(id);
const state = { tab: "health", caseId: "" };
const loaders = {};
const token = () => $("token").value.trim();

function headers(extra) {
  const h = Object.assign({"Accept": "application/json"}, extra || {});
  if (token()) h["Authorization"] = "Bearer " + token();
  return h;
}
function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined && text !== null) n.textContent = String(text);
  if (cls) n.className = cls;
  return n;
}
function setStatus(msg, isErr) {
  $("status").textContent = msg || "";
  $("status").className = isErr ? "err" : "muted";
}
function showError(node, e) {
  node.className = "err";
  node.textContent = String((e && e.message) || e);
}
function when(ts) { return ts ? new Date(ts * 1000).toLocaleString() : ""; }
function preText(id, obj) {
  $(id).textContent = typeof obj === "string" ? obj : JSON.stringify(obj, null, 1);
}
function render(id, node) {
  const box = $(id);
  box.className = "";
  box.replaceChildren(node);
}
function actionTable(rows, cols, actions) {
  const t = document.createElement("table");
  const thead = document.createElement("thead");
  const tr = document.createElement("tr");
  cols.forEach((c) => tr.appendChild(el("th", c)));
  if (actions && actions.length) tr.appendChild(el("th", "操作"));
  thead.appendChild(tr);
  t.appendChild(thead);
  const tb = document.createElement("tbody");
  rows.forEach((r, i) => {
    const row = document.createElement("tr");
    cols.forEach((c) => {
      const v = r[c];
      row.appendChild(el("td", v === null || v === undefined ? "" : String(v).slice(0, 160)));
    });
    if (actions && actions.length) {
      const td = el("td", null, "actions");
      actions.forEach((a) => {
        const b = el("button", a.label);
        b.addEventListener("click", () => a.fn(r, i));
        td.appendChild(b);
      });
      row.appendChild(td);
    }
    tb.appendChild(row);
  });
  t.appendChild(tb);
  return t;
}
function table(rows, cols) { return actionTable(rows, cols, null); }
async function get(path) {
  const r = await fetch(path, {headers: headers()});
  if (!r.ok) throw new Error(path + " -> HTTP " + r.status);
  return r.json();
}
async function post(path, payload) {
  const r = await fetch(path, {
    method: "POST",
    headers: headers({"Content-Type": "application/json"}),
    body: JSON.stringify(payload || {})
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || (path + " -> HTTP " + r.status));
  return data;
}

/* ---------- 90.1 运行健康 ---------- */
loaders.health = async () => {
  setStatus("加载中…");
  try {
    const [health, metrics, alerts, tasks] = await Promise.all([
      get("/health"), get("/metrics"), get("/alerts"), get("/tasks")
    ]);
    preText("health", health);
    const t = metrics.tasks || {};
    const by = t.by_status || {};
    render("healthTasks", table([{
      total: t.total, succeeded: by.SUCCEEDED || 0, failed: by.FAILED || 0,
      success_rate: t.success_rate, avg_latency_s: t.avg_latency_s,
      consecutive_failures: t.consecutive_failures
    }], ["total", "succeeded", "failed", "success_rate", "avg_latency_s",
         "consecutive_failures"]));
    render("healthBudget", table(
      Object.entries(metrics.budget || {}).map(([scope, v]) => ({
        scope: scope, model_calls: v.model_calls, tool_calls: v.tool_calls,
        cost: v.cost, seconds: v.seconds
      })), ["scope", "model_calls", "tool_calls", "cost", "seconds"]));
    render("healthAlerts", table((alerts.alerts || []).map((a) => ({
      rule: a.rule, severity: a.severity, message: a.message,
      value: a.value, threshold: a.threshold
    })), ["rule", "severity", "message", "value", "threshold"]));
    render("healthTools", table(
      Object.entries(metrics.tools_health || {}).map(([tool, ok]) => ({
        tool: tool, status: ok ? "healthy" : "down"
      })), ["tool", "status"]));
    render("healthRecent", table((tasks.tasks || []).slice(0, 20).map((x) => ({
      id: x.task_id, status: x.task_status, intent: x.intent,
      goal: x.user_goal, failure: x.failure_reason || ""
    })), ["id", "status", "intent", "goal", "failure"]));
    $("stamp").textContent = "更新于 " + new Date().toLocaleString();
    setStatus("");
  } catch (e) { setStatus(e.message, true); showError($("health"), e); }
};

/* ---------- 90.2 问题中心 ---------- */
function openCase(id) {
  state.caseId = id;
  $("caseId").value = id;
  showTab("case");
}
loaders.problems = async () => {
  setStatus("加载中…");
  try {
    const [cases, trends, drift] = await Promise.all([
      get("/diagnostics?limit=50"), get("/diagnostics/trends?limit=200"),
      get("/diagnostics/drift")
    ]);
    render("diagCases", actionTable((cases.cases || []).map((c) => ({
      case_id: c.case_id, task_id: c.task_id, status: c.status,
      symptom: c.symptom, created: when(c.created_at)
    })), ["case_id", "task_id", "status", "symptom", "created"], [
      {label: "详情", fn: (row) => openCase(row.case_id)}
    ]));
    preText("diagTrends", trends);
    preText("diagDrift", drift);
    setStatus("");
  } catch (e) { setStatus(e.message, true); showError($("diagCases"), e); }
};
$("diagScan").addEventListener("click", async () => {
  $("diagScanOut").textContent = "扫描中…";
  try {
    const out = await post("/diagnostics/scan", {});
    $("diagScanOut").textContent =
      "新建案例 " + (out.cases || []).length + "，跳过 " + (out.skipped || 0);
    loaders.problems();
  } catch (e) { $("diagScanOut").textContent = String(e.message || e); }
});
$("driftScan").addEventListener("click", async () => {
  $("diagDrift").textContent = "检测中…";
  try {
    preText("diagDrift", await post("/diagnostics/drift/scan", {}));
  } catch (e) { $("diagDrift").textContent = String(e.message || e); }
});

/* ---------- 90.3 诊断详情 ---------- */
loaders.case = async () => {
  const id = $("caseId").value.trim() || state.caseId;
  if (!id) { $("caseInfo").textContent = "请输入 case_id 或在问题中心选择案例"; return; }
  state.caseId = id;
  setStatus("加载中…");
  try {
    const out = await get("/diagnostics/" + encodeURIComponent(id));
    const c = out.case || {};
    render("caseInfo", table([{
      case_id: c.case_id, task_id: c.task_id, run_id: c.run_id,
      status: c.status, symptom: c.symptom, candidate_id: c.candidate_id || "",
      created: when(c.created_at), updated: when(c.updated_at)
    }], ["case_id", "task_id", "run_id", "status", "symptom",
         "candidate_id", "created", "updated"]));
    preText("caseEvidence", c.evidence_snapshot || {});
    render("caseHypotheses", actionTable((out.hypotheses || []).map((h) => ({
      category: h.category, component: h.component, statement: h.statement,
      support: (h.supporting_evidence || []).join(" | "),
      opposing: (h.opposing_evidence || []).join(" | "),
      confidence: h.confidence, uncertainty: h.uncertainty,
      source: h.source, status: h.status
    })), ["category", "component", "statement", "support", "opposing",
         "confidence", "uncertainty", "source", "status"], [
      {label: "运行探针", fn: (row, i) => runProbe(out.hypotheses[i].hypothesis_id)},
      {label: "升级候选", fn: (row, i) => promoteHypothesis(out.hypotheses[i].hypothesis_id)}
    ]));
    render("caseExperiments", table((out.experiments || []).map((x) => ({
      experiment_id: x.experiment_id, hypothesis_id: x.hypothesis_id,
      method: x.method, result: x.result, cost: x.cost,
      finished: when(x.finished_at)
    })), ["experiment_id", "hypothesis_id", "method", "result", "cost", "finished"]));
    setStatus("");
  } catch (e) { setStatus(e.message, true); showError($("caseInfo"), e); }
};
async function runProbe(hypothesisId) {
  const method = $("probeSelect").value;
  const fault = $("faultSelect").value;
  $("caseOut").textContent = "运行探针 " + method + "…";
  try {
    const out = await post(
      "/diagnostics/" + encodeURIComponent(state.caseId) + "/experiments",
      {method: method, hypothesis_id: hypothesisId, fault: fault || undefined});
    $("caseOut").textContent = JSON.stringify(out, null, 1);
    loaders.case();
  } catch (e) { $("caseOut").textContent = String(e.message || e); }
}
async function promoteHypothesis(hypothesisId) {
  $("caseOut").textContent = "升级中…";
  try {
    const out = await post(
      "/diagnostics/" + encodeURIComponent(state.caseId) + "/candidates",
      {hypothesis_id: hypothesisId});
    $("caseOut").textContent = JSON.stringify(out, null, 1);
    loaders.case();
  } catch (e) { $("caseOut").textContent = String(e.message || e); }
}
$("caseLoad").addEventListener("click", () => loaders.case());
$("caseId").addEventListener("keydown", (ev) => { if (ev.key === "Enter") loaders.case(); });
$("caseSuggest").addEventListener("click", async () => {
  if (!state.caseId && !$("caseId").value.trim()) return;
  const id = $("caseId").value.trim() || state.caseId;
  $("caseOut").textContent = "生成模型辅助假设…";
  try {
    const out = await post(
      "/diagnostics/" + encodeURIComponent(id) + "/hypotheses",
      {max_items: 3});
    $("caseOut").textContent = JSON.stringify(out, null, 1);
    loaders.case();
  } catch (e) { $("caseOut").textContent = String(e.message || e); }
});

/* ---------- 90.4 候选实验中心 ---------- */
async function candidateAction(id, action, extra) {
  $("candOut").textContent = action + "…";
  try {
    const out = await post("/evolution/candidates/" + encodeURIComponent(id) + "/" + action,
                           extra || {});
    $("candOut").textContent = JSON.stringify(out, null, 1);
    loaders.candidates();
  } catch (e) { $("candOut").textContent = String(e.message || e); }
}
loaders.candidates = async () => {
  setStatus("加载中…");
  try {
    const status = $("candStatus").value;
    const out = await get("/evolution/candidates?limit=50" +
                          (status ? "&status=" + encodeURIComponent(status) : ""));
    render("candTable", actionTable((out.candidates || []).map((c) => ({
      candidate_id: c.candidate_id, status: c.status, risk: c.risk_level,
      component: c.target_component, problem: c.target_problem,
      suggestion: c.publish_suggestion
    })), ["candidate_id", "status", "risk", "component", "problem", "suggestion"], [
      {label: "详情", fn: async (row) => {
        try { preText("candOut", await get("/evolution/candidates/" +
                                           encodeURIComponent(row.candidate_id))); }
        catch (e) { $("candOut").textContent = String(e.message || e); }
      }},
      {label: "评测", fn: (row) => candidateAction(row.candidate_id, "evaluate",
                                                   {baseline: true})},
      {label: "批准", fn: (row) => candidateAction(row.candidate_id, "approve",
                                                   {approver: "admin-ui"})},
      {label: "灰度发布", fn: (row) => {
        if (!window.confirm("发布候选 " + row.candidate_id + "？")) return;
        candidateAction(row.candidate_id, "deploy",
                        {actor: "admin-ui", gray_percent: 100});
      }},
      {label: "监测", fn: (row) => candidateAction(row.candidate_id, "monitor", {})},
      {label: "回滚", fn: (row) => {
        if (!window.confirm("回滚候选 " + row.candidate_id + " 及其发布？")) return;
        candidateAction(row.candidate_id, "rollback", {reason: "admin-ui rollback"});
      }}
    ]));
    setStatus("");
  } catch (e) { setStatus(e.message, true); showError($("candTable"), e); }
};
$("candStatus").addEventListener("change", () => loaders.candidates());

/* ---------- 90.5 版本中心 ---------- */
async function migrationAction(id, action) {
  if ((action === "apply" || action === "rollback") &&
      !window.confirm("确认对迁移 " + id + " 执行 " + action + "？")) return;
  $("versionOut").textContent = action + "…";
  try {
    const payload = action === "rollback" ? {reason: "admin-ui rollback"} : {};
    const out = await post("/models/migrations/" + encodeURIComponent(id) + "/" + action,
                           payload);
    $("versionOut").textContent = JSON.stringify(out, null, 1);
    loaders.versions();
  } catch (e) { $("versionOut").textContent = String(e.message || e); }
}
loaders.versions = async () => {
  setStatus("加载中…");
  try {
    const [rel, models, migs, wfs, wfcands] = await Promise.all([
      get("/releases"), get("/models"), get("/models/migrations?limit=50"),
      get("/workflows"), get("/workflows/candidates?limit=50")
    ]);
    preText("versionSummary", {
      current: rel.current, deployments: rel.deployments,
      rollbacks: rel.rollbacks
    });
    render("versionReleases", table((rel.manifests || []).map((m) => ({
      release_id: m.release_id, version: m.version, commit: m.code_commit,
      adapter: m.model_adapter_version, digest: m.artifact_digest,
      created: when(m.created_at)
    })), ["release_id", "version", "commit", "adapter", "digest", "created"]));
    render("versionModels", table((models.models || []).map((m) => ({
      model_id: m.model_id, provider: m.provider, context: m.context_limit,
      quality: m.quality_score, latency: m.avg_latency_s,
      price_in: m.price_per_1k_input, price_out: m.price_per_1k_output,
      available: m.available
    })), ["model_id", "provider", "context", "quality", "latency",
         "price_in", "price_out", "available"]));
    render("versionMigrations", actionTable((migs.migrations || []).map((m) => ({
      migration_id: m.migration_id, status: m.status,
      old: m.old_model_id, new: m.new_model_id,
      dataset: m.dataset_id || "", created: when(m.created_at)
    })), ["migration_id", "status", "old", "new", "dataset", "created"], [
      {label: "回归", fn: (row) => migrationAction(row.migration_id, "regression")},
      {label: "发布组合", fn: (row) => migrationAction(row.migration_id, "release")},
      {label: "启用", fn: (row) => migrationAction(row.migration_id, "apply")},
      {label: "回滚", fn: (row) => migrationAction(row.migration_id, "rollback")}
    ]));
    render("versionWorkflows", table((wfs.workflows || []).map((w) => ({
      workflow_id: w.workflow_id, version: w.version, status: w.status,
      updated: when(w.updated_at)
    })), ["workflow_id", "version", "status", "updated"]));
    render("versionWfCands", table((wfcands.candidates || []).map((c) => ({
      candidate_id: c.candidate_id || c.id || "", status: c.status || "",
      intent: c.intent || "", support: c.support || c.support_count || "",
      workflow_id: c.workflow_id || ""
    })), ["candidate_id", "status", "intent", "support", "workflow_id"]));
    setStatus("");
  } catch (e) { setStatus(e.message, true); showError($("versionSummary"), e); }
};
$("migCreate").addEventListener("click", async () => {
  $("versionOut").textContent = "创建中…";
  try {
    const out = await post("/models/migrations", {
      old_model_id: $("migOld").value.trim(),
      new_model_id: $("migNew").value.trim(),
      dataset_id: $("migDataset").value.trim() || undefined
    });
    $("versionOut").textContent = JSON.stringify(out, null, 1);
    loaders.versions();
  } catch (e) { $("versionOut").textContent = String(e.message || e); }
});
$("wfExtract").addEventListener("click", async () => {
  $("versionOut").textContent = "抽取中…";
  try {
    const out = await post("/workflows/extract", {});
    $("versionOut").textContent = JSON.stringify(out, null, 1);
    loaders.versions();
  } catch (e) { $("versionOut").textContent = String(e.message || e); }
});

/* ---------- 90.6 成本中心 ---------- */
loaders.costs = async () => {
  setStatus("加载中…");
  try {
    const out = await get("/costs?limit=500");
    render("costTotals", table([out.totals || {}],
      ["tasks", "model_calls", "tool_calls", "cost", "successful_tasks",
       "failed_tasks", "cost_per_success", "cost_per_success_including_waste"]));
    const groupRows = (obj) => Object.entries(obj || {}).map(([k, v]) => ({
      key: k, tasks: v.tasks, cost: v.cost, model_calls: v.model_calls,
      tool_calls: v.tool_calls, success: v.successful_tasks,
      cost_per_success: v.cost_per_success
    }));
    const cols = ["key", "tasks", "cost", "model_calls", "tool_calls",
                  "success", "cost_per_success"];
    render("costDomain", table(groupRows(out.by_domain), cols));
    render("costModel", table(groupRows(out.by_model), cols));
    render("costVersion", table(groupRows(out.by_version), cols));
    preText("costEvolution", out.evolution || {});
    render("costTasks", table((out.by_task || []).slice(0, 50).map((x) => ({
      task_id: x.task_id, intent: x.intent, status: x.status,
      cost: x.cost, model_calls: x.model_calls, tool_calls: x.tool_calls
    })), ["task_id", "intent", "status", "cost", "model_calls", "tool_calls"]));
    setStatus("");
  } catch (e) { setStatus(e.message, true); showError($("costTotals"), e); }
};

/* ---------- 90.7 审计与回滚中心 ---------- */
loaders.audit = async () => {
  setStatus("加载中…");
  try {
    const [audit, layers, rel] = await Promise.all([
      get("/audit?limit=200"), get("/logs/layers"), get("/releases")
    ]);
    $("auditCurrent").textContent = rel.current || "（无）";
    render("auditTable", table((audit.audit || []).map((a) => ({
      at: when(a.at), actor: a.actor, action: a.action, target: a.target,
      outcome: a.outcome || "",
      detail: a.detail ? JSON.stringify(a.detail).slice(0, 160) : ""
    })), ["at", "actor", "action", "target", "outcome", "detail"]));
    render("auditLayers", table((layers.layers || []).map((l) => ({
      layer: l.layer, retention_days: l.retention_days,
      roles: (l.roles || []).join(","), count: l.count,
      readable: l.readable
    })), ["layer", "retention_days", "roles", "count", "readable"]));
    setStatus("");
  } catch (e) { setStatus(e.message, true); showError($("auditTable"), e); }
};
$("releaseRollback").addEventListener("click", async () => {
  const reason = $("rollbackReason").value.trim() || "admin-ui rollback";
  if (!window.confirm("回滚当前发布（原因：" + reason + "）？")) return;
  $("auditOut").textContent = "回滚中…";
  try {
    $("auditOut").textContent = JSON.stringify(
      await post("/releases/rollback", {reason: reason}), null, 1);
    loaders.audit();
  } catch (e) { $("auditOut").textContent = String(e.message || e); }
});

/* ---------- 任务回放 ---------- */
$("replayBtn").addEventListener("click", async () => {
  const id = $("replayTask").value.trim();
  if (!id) return;
  $("replayOut").textContent = "加载中…";
  try {
    const out = await get("/tasks/" + encodeURIComponent(id) + "/replay");
    $("replayOut").textContent = JSON.stringify({
      task: out.task_id, status: out.task_status,
      read_only: out.read_only,
      reexecute_side_effects: out.reexecute_side_effects,
      versions: out.versions, stats: out.stats,
      timeline: out.timeline.map(e => ({
        at: when(e.at), source: e.source,
        kind: e.kind, step: e.step, privacy: e.privacy_classification,
        payload: e.payload
      }))
    }, null, 1);
  } catch (e) {
    $("replayOut").textContent = String(e.message || e);
  }
});

/* ---------- 备份与恢复 ---------- */
async function loadBackups() {
  try {
    const out = await get("/admin/backups");
    $("backups").className = "";
    $("backups").replaceChildren(table((out.backups || []).map(b => ({
      name: b.name,
      created: when(b.created_at),
      bytes: b.bytes,
      tasks: (b.counts || {}).tasks,
      events: (b.counts || {}).events
    })), ["name", "created", "bytes", "tasks", "events"]));
  } catch (e) {
    showError($("backups"), e);
  }
}
$("backupCreate").addEventListener("click", async () => {
  $("backupOut").textContent = "创建中…";
  try {
    $("backupOut").textContent = JSON.stringify(
      await post("/admin/backup", {}), null, 1);
    loadBackups();
  } catch (e) { $("backupOut").textContent = String(e.message || e); }
});
$("restoreBtn").addEventListener("click", async () => {
  const name = $("restoreName").value.trim();
  if (!name) return;
  if (!window.confirm("恢复会覆盖当前数据（先自动创建安全备份），确认继续？")) return;
  $("backupOut").textContent = "恢复中…";
  try {
    $("backupOut").textContent = JSON.stringify(
      await post("/admin/restore", {name}), null, 1);
    loadBackups();
  } catch (e) { $("backupOut").textContent = String(e.message || e); }
});

/* ---------- 隔离沙箱 ---------- */
$("sandboxRun").addEventListener("click", async () => {
  const code = $("sandboxCode").value;
  if (!code.trim()) return;
  if (!window.confirm("在隔离沙箱中执行这段候选代码？")) return;
  $("sandboxOut").textContent = "执行中…";
  try {
    $("sandboxOut").textContent = JSON.stringify(await post("/evolution/sandbox/run", {
      code: code, timeout: Number($("sandboxTimeout").value) || 5
    }), null, 1);
  } catch (e) { $("sandboxOut").textContent = String(e.message || e); }
});

/* ---------- 导航 ---------- */
function showTab(name) {
  document.querySelectorAll("nav button").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === name);
  });
  document.querySelectorAll(".panel").forEach((p) => {
    p.classList.toggle("active", p.id === "panel-" + name);
  });
  state.tab = name;
  if (loaders[name]) loaders[name]();
  if (name === "backup") loadBackups();
}
document.querySelectorAll("nav button").forEach((b) => {
  b.addEventListener("click", () => showTab(b.dataset.tab));
});
$("refresh").addEventListener("click", () => showTab(state.tab));
$("token").addEventListener("change", () => {
  sessionStorage.setItem("evo_token", token());
  showTab(state.tab);
});
$("token").value = sessionStorage.getItem("evo_token") || "";
showTab("health");
</script>
</body>
</html>
"""
