from __future__ import annotations


ADMIN_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CloudAgent Platform</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d9dde5;
      --text: #18202b;
      --muted: #596579;
      --accent: #1967d2;
      --ok: #188038;
      --warn: #b06000;
      --bad: #b3261e;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      min-height: 64px;
      padding: 0 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    h1 { font-size: 18px; margin: 0; font-weight: 650; }
    main { padding: 20px 24px 28px; }
    .toolbar {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }
    input, select, button {
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 10px;
      background: #fff;
      color: var(--text);
    }
    button {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
      cursor: pointer;
      font-weight: 600;
    }
    button.secondary {
      background: #fff;
      color: var(--text);
      border-color: var(--line);
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
      gap: 1px;
      border: 1px solid var(--line);
      background: var(--line);
      margin-bottom: 20px;
    }
    .metric {
      background: var(--panel);
      padding: 14px;
      min-height: 76px;
    }
    .metric .label { color: var(--muted); font-size: 12px; }
    .metric .value { font-size: 24px; font-weight: 700; margin-top: 6px; }
    .grid {
      display: grid;
      grid-template-columns: minmax(280px, 0.95fr) minmax(360px, 1.3fr);
      gap: 20px;
      align-items: start;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      margin-bottom: 20px;
    }
    section h2 {
      margin: 0;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      font-size: 14px;
      background: #fbfcfe;
    }
    table {
      width: 100%;
      border-collapse: collapse;
    }
    th, td {
      text-align: left;
      padding: 9px 12px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }
    th { color: var(--muted); font-size: 12px; font-weight: 650; }
    td code {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 12px;
      color: #303846;
    }
    .form {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      padding: 12px;
      border-bottom: 1px solid var(--line);
    }
    .form button { grid-column: span 2; }
    .status-ok { color: var(--ok); font-weight: 650; }
    .status-warn { color: var(--warn); font-weight: 650; }
    pre {
      margin: 0;
      padding: 12px;
      overflow: auto;
      max-height: 320px;
      background: #101721;
      color: #dce7f7;
      font-size: 12px;
    }
    @media (max-width: 900px) {
      .metrics { grid-template-columns: repeat(2, minmax(110px, 1fr)); }
      .grid { grid-template-columns: 1fr; }
      header { align-items: flex-start; flex-direction: column; padding: 14px; }
      main { padding: 14px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>CloudAgent Platform</h1>
    <div class="toolbar">
      <input id="token" placeholder="API token" autocomplete="off" aria-label="API token">
      <button class="secondary" onclick="refresh()">Refresh</button>
      <button onclick="seed()">Seed Agent</button>
    </div>
  </header>
  <main>
    <div class="metrics" id="metrics"></div>
    <div class="grid">
      <div>
        <section>
          <h2>Workers</h2>
          <div class="form">
            <input id="workerName" placeholder="worker name">
            <input id="workerId" placeholder="optional worker id">
            <button onclick="registerWorker()">Register Worker</button>
          </div>
          <table>
            <thead><tr><th>Worker</th><th>Status</th><th>Active Run</th><th></th></tr></thead>
            <tbody id="workers"></tbody>
          </table>
        </section>
        <section>
          <h2>Create Integration</h2>
          <div class="form">
            <select id="provider">
              <option value="feishu">Feishu</option>
              <option value="dify">Dify</option>
              <option value="github_actions">GitHub Actions</option>
              <option value="webhook">Webhook</option>
            </select>
            <input id="baseUrl" placeholder="base URL">
            <input id="integrationName" placeholder="name">
            <input id="secret" placeholder="token/secret">
            <button onclick="createIntegration()">Create Integration</button>
          </div>
          <table>
            <thead><tr><th>Provider</th><th>Status</th><th>Capabilities</th></tr></thead>
            <tbody id="integrations"></tbody>
          </table>
        </section>
        <section>
          <h2>Create Job</h2>
          <div class="form">
            <input id="jobName" placeholder="job name">
            <select id="delay">
              <option value="0">manual</option>
              <option value="5">delay 5s</option>
              <option value="30">delay 30s</option>
            </select>
            <button onclick="createJob()">Create Job</button>
          </div>
          <table>
            <thead><tr><th>Name</th><th>Status</th><th>Next Run</th><th></th></tr></thead>
            <tbody id="jobs"></tbody>
          </table>
        </section>
      </div>
      <div>
        <section>
          <h2>Recent Runs</h2>
          <table>
            <thead><tr><th>Run</th><th>Status</th><th>Worker</th><th>Source</th></tr></thead>
            <tbody id="runs"></tbody>
          </table>
        </section>
        <section>
          <h2>Pending Actions</h2>
          <table>
            <thead><tr><th>Action</th><th>Tool</th><th>Status</th></tr></thead>
            <tbody id="pendingActions"></tbody>
          </table>
        </section>
        <section>
          <h2>Recent Artifacts</h2>
          <table>
            <thead><tr><th>Artifact</th><th>Name</th><th>Size</th></tr></thead>
            <tbody id="artifacts"></tbody>
          </table>
        </section>
        <section>
          <h2>Recent Sessions</h2>
          <table>
            <thead><tr><th>Session</th><th>Status</th><th>Updated</th></tr></thead>
            <tbody id="sessions"></tbody>
          </table>
        </section>
        <section>
          <h2>API Output</h2>
          <pre id="output"></pre>
        </section>
      </div>
    </div>
  </main>
  <script>
    const headers = () => ({
      "Content-Type": "application/json",
      "Authorization": `Bearer ${document.getElementById("token").value}`
    });
    const api = async (path, options = {}) => {
      const res = await fetch(path, { ...options, headers: { ...headers(), ...(options.headers || {}) } });
      const text = await res.text();
      let data;
      try { data = JSON.parse(text); } catch { data = text; }
      document.getElementById("output").textContent = JSON.stringify(data, null, 2);
      if (!res.ok) throw new Error(data?.error?.message || res.statusText);
      return data;
    };
    const escapeHtml = value => String(value ?? "").replace(
      /[&<>"']/g,
      ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch])
    );
    const code = value => `<code>${escapeHtml(value)}</code>`;
    const cells = row => row.map(value => `<td>${value ?? ""}</td>`).join("");
    async function refresh() {
      const data = await api("/api/v1/admin/overview");
      const counts = data.counts || {};
      document.getElementById("metrics").innerHTML = Object.entries(counts)
        .map(([k, v]) => `<div class="metric"><div class="label">${escapeHtml(k)}</div><div class="value">${escapeHtml(v)}</div></div>`)
        .join("");
      document.getElementById("workers").innerHTML = (data.workers || [])
        .map(item => `<tr>${cells([
          code(item.id),
          escapeHtml(item.status),
          item.active_run_id ? code(item.active_run_id) : "",
          `<button class="secondary" onclick="claimRun('${item.id}')">Claim</button>`
        ])}</tr>`)
        .join("");
      document.getElementById("integrations").innerHTML = (data.integrations || [])
        .map(item => `<tr>${cells([
          escapeHtml(item.provider),
          `<span class="${item.status === "configured" ? "status-ok" : "status-warn"}">${escapeHtml(item.status)}</span>`,
          Object.keys(item.capabilities || {}).filter(k => item.capabilities[k] === true).map(escapeHtml).join(", ")
        ])}</tr>`)
        .join("");
      document.getElementById("jobs").innerHTML = (data.recent_jobs || [])
        .map(item => `<tr>${cells([
          escapeHtml(item.name),
          escapeHtml(item.status),
          escapeHtml(item.next_run_at || ""),
          `<button class="secondary" onclick="triggerJob('${item.id}')">Run</button> <button class="secondary" onclick="enqueueJob('${item.id}')">Queue</button>`
        ])}</tr>`)
        .join("");
      document.getElementById("sessions").innerHTML = (data.recent_sessions || [])
        .map(item => `<tr>${cells([code(item.id), escapeHtml(item.status), escapeHtml(item.updated_at)])}</tr>`)
        .join("");
      document.getElementById("runs").innerHTML = (data.recent_runs || [])
        .map(item => `<tr>${cells([
          code(item.id),
          escapeHtml(item.status),
          item.worker_id ? code(item.worker_id) : "",
          escapeHtml(item.trigger_source)
        ])}</tr>`)
        .join("");
      document.getElementById("pendingActions").innerHTML = (data.pending_actions || [])
        .map(item => `<tr>${cells([code(item.id), escapeHtml(item.tool), escapeHtml(item.status)])}</tr>`)
        .join("");
      document.getElementById("artifacts").innerHTML = (data.recent_artifacts || [])
        .map(item => `<tr>${cells([code(item.id), escapeHtml(item.name), escapeHtml(item.size)])}</tr>`)
        .join("");
    }
    async function seed() {
      await api("/api/v1/agents", {
        method: "POST",
        body: JSON.stringify({ name: "Local operator", kernel: { id: "codex-cli-local" }, system: "Operate within policy." })
      });
      await refresh();
    }
    async function createIntegration() {
      await api("/api/v1/integrations", {
        method: "POST",
        body: JSON.stringify({
          provider: document.getElementById("provider").value,
          name: document.getElementById("integrationName").value,
          base_url: document.getElementById("baseUrl").value,
          token: document.getElementById("secret").value
        })
      });
      await refresh();
    }
    async function registerWorker() {
      const payload = {
        name: document.getElementById("workerName").value || "Local worker",
        capabilities: { local_noop_turn: true, session_events: true, integration_webhooks: true }
      };
      const workerId = document.getElementById("workerId").value;
      if (workerId) payload.id = workerId;
      await api("/api/v1/workers", {
        method: "POST",
        body: JSON.stringify(payload)
      });
      await refresh();
    }
    async function createJob() {
      const delay = Number(document.getElementById("delay").value);
      await api("/api/v1/jobs", {
        method: "POST",
        body: JSON.stringify({
          name: document.getElementById("jobName").value || "Manual cloud agent run",
          trigger: delay > 0 ? { type: "delay", delay_seconds: delay } : { type: "manual" }
        })
      });
      await refresh();
    }
    async function triggerJob(id) {
      await api(`/api/v1/jobs/${id}/trigger`, { method: "POST", body: "{}" });
      await refresh();
    }
    async function enqueueJob(id) {
      await api(`/api/v1/jobs/${id}/enqueue`, { method: "POST", body: "{}" });
      await refresh();
    }
    async function claimRun(workerId) {
      await api(`/api/v1/workers/${workerId}/claim`, { method: "POST", body: "{}" });
      await refresh();
    }
    refresh().catch(err => document.getElementById("output").textContent = err.message);
  </script>
</body>
</html>
"""
