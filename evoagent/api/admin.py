"""Web 管理端（PRD 90 的最小可用子集）。

只读观测面板：健康、指标、任务、诊断、进化候选、发布与审计。
所有动态内容用 textContent 写入，避免把服务端数据当 HTML 注入。
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
  header { padding: 14px 20px; border-bottom: 1px solid var(--line);
           display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  h1 { font-size: 17px; margin: 0; }
  main { padding: 16px 20px; display: grid; gap: 18px;
         grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); }
  section { border: 1px solid var(--line); border-radius: 8px; padding: 12px; }
  h2 { font-size: 14px; margin: 0 0 8px; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { text-align: left; padding: 4px 6px; border-bottom: 1px solid var(--line);
           vertical-align: top; }
  code, pre { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
  pre { white-space: pre-wrap; word-break: break-word; max-height: 320px;
        overflow: auto; }
  input, button { font: inherit; padding: 4px 8px; }
  .muted { opacity: .7; }
  .err { color: #d33; }
</style>
</head>
<body>
<header>
  <h1>EvoAgent 管理端</h1>
  <span class="muted" id="stamp"></span>
  <input id="token" type="password" placeholder="API Token（如已启用鉴权）" size="28">
  <button id="refresh">刷新</button>
  <span id="status" class="muted"></span>
</header>
<main>
  <section><h2>健康</h2><pre id="health">-</pre></section>
  <section><h2>指标</h2><pre id="metrics">-</pre></section>
  <section><h2>任务</h2><div id="tasks"></div></section>
  <section><h2>进化候选</h2><div id="candidates"></div></section>
  <section><h2>诊断案例</h2><div id="diagnostics"></div></section>
  <section><h2>发布与审计</h2><pre id="releases">-</pre><div id="audit"></div></section>
</main>
<script>
const $ = (id) => document.getElementById(id);
const token = () => $("token").value.trim();
function headers(extra) {
  const h = Object.assign({"Accept": "application/json"}, extra || {});
  if (token()) h["Authorization"] = "Bearer " + token();
  return h;
}
function table(rows, cols) {
  const t = document.createElement("table");
  const thead = document.createElement("thead");
  const tr = document.createElement("tr");
  cols.forEach((c) => { const th = document.createElement("th"); th.textContent = c; tr.appendChild(th); });
  thead.appendChild(tr); t.appendChild(thead);
  const tb = document.createElement("tbody");
  rows.forEach((r) => {
    const row = document.createElement("tr");
    cols.forEach((c) => {
      const td = document.createElement("td");
      const v = r[c];
      td.textContent = v === null || v === undefined ? "" : String(v).slice(0, 120);
      row.appendChild(td);
    });
    tb.appendChild(row);
  });
  t.appendChild(tb);
  return t;
}
async function get(path) {
  const r = await fetch(path, {headers: headers()});
  if (!r.ok) throw new Error(path + " -> HTTP " + r.status);
  return r.json();
}
async function refresh() {
  $("status").textContent = "加载中…";
  try {
    const [health, metrics, tasks, cands, diags, releases, audit] = await Promise.all([
      get("/health"), get("/metrics"), get("/tasks?limit=20"),
      get("/evolution/candidates?limit=20"), get("/diagnostics?limit=20"),
      get("/releases"), get("/audit?limit=20")
    ]);
    $("health").textContent = JSON.stringify(health, null, 1);
    $("metrics").textContent = JSON.stringify(metrics.tasks, null, 1);
    $("tasks").replaceChildren(table(tasks.tasks.map(t => ({
      id: t.task_id, status: t.task_status, intent: t.intent,
      goal: t.user_goal, failure: t.failure_reason || ""
    })), ["id", "status", "intent", "goal", "failure"]));
    $("candidates").replaceChildren(table(cands.candidates.map(c => ({
      id: c.candidate_id, status: c.status, risk: c.risk_level,
      target: c.target_component, problem: c.target_problem
    })), ["id", "status", "risk", "target", "problem"]));
    $("diagnostics").replaceChildren(table(diags.cases.map(c => ({
      id: c.case_id, task: c.task_id, status: c.status, symptom: c.symptom
    })), ["id", "task", "status", "symptom"]));
    $("releases").textContent = JSON.stringify({
      current: releases.current,
      deployments: releases.deployments,
      rollbacks: releases.rollbacks
    }, null, 1);
    $("audit").replaceChildren(table(audit.audit.map(a => ({
      at: new Date(a.at * 1000).toLocaleString(), actor: a.actor,
      action: a.action, target: a.target, outcome: a.outcome
    })), ["at", "actor", "action", "target", "outcome"]));
    $("stamp").textContent = new Date().toLocaleString();
    $("status").textContent = "";
    $("status").className = "muted";
  } catch (e) {
    $("status").textContent = String(e.message || e);
    $("status").className = "err";
  }
}
$("refresh").addEventListener("click", refresh);
$("token").addEventListener("change", () => sessionStorage.setItem("evo_token", token()));
$("token").value = sessionStorage.getItem("evo_token") || "";
refresh();
</script>
</body>
</html>
"""
