/* CloudAgent Console: dependency-free, same-origin administrator client. */
(() => {
  "use strict";
  const TOKEN_KEY = "cloudagent.console.token";
  const VIEW_KEY = "cloudagent.console.view";
  const views = ["overview", "runtime", "sessions", "governance", "resources"];
  const labels = {
    overview: "Overview",
    runtime: "Runtime",
    sessions: "Sessions",
    governance: "Governance",
    resources: "Resources",
  };
  const state = {
    token: sessionStorage.getItem(TOKEN_KEY) || "",
    view: sessionStorage.getItem(VIEW_KEY) || "overview",
    overview: null,
    data: {},
    busy: new Set(),
    loadEpoch: 0,
  };
  let dialogReturnFocus = null;
  let dialogFocusSelector = null;
  let dialogEpoch = 0;
  const app = document.querySelector("#app");
  const dialogRoot = document.querySelector("#dialog-root");
  const toastRoot = document.querySelector("#toast-region");
  const escapeHtml = (value) =>
    String(value ?? "").replace(
      /[&<>'\"]/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          "'": "&#39;",
          '"': "&quot;",
        })[c],
    );
  const id = (value) =>
    value
      ? `<button class="copy" type="button" data-copy="${escapeHtml(value)}" title="Copy identifier">${escapeHtml(value)}</button>`
      : '<span class="muted">—</span>';
  const listData = (value) =>
    Array.isArray(value) ? value : Array.isArray(value?.data) ? value.data : [];
  const items = (value) =>
    Array.isArray(value)
      ? value
      : Array.isArray(value?.data)
        ? value.data
        : Array.isArray(value?.items)
          ? value.items
          : [];
  const when = (value) => {
    if (!value) return "—";
    const seconds = Math.round((Date.now() - new Date(value).getTime()) / 1000);
    if (!Number.isFinite(seconds)) return escapeHtml(value);
    const abs = Math.abs(seconds);
    const unit =
      abs < 60
        ? [abs, "s"]
        : abs < 3600
          ? [Math.round(abs / 60), "m"]
          : abs < 86400
            ? [Math.round(abs / 3600), "h"]
            : [Math.round(abs / 86400), "d"];
    return seconds >= 0
      ? `${unit[0]}${unit[1]} ago`
      : `in ${unit[0]}${unit[1]}`;
  };
  const payloadView = (payload) => {
    const compact = JSON.stringify(payload || {});
    if (compact.length <= 180)
      return `<span class="micro mono">${escapeHtml(compact)}</span>`;
    const summary = `${compact.slice(0, 176)}…`;
    return `<details class="event-details"><summary>${escapeHtml(summary)}</summary><pre>${escapeHtml(JSON.stringify(payload || {}, null, 2))}</pre></details>`;
  };
  const badge = (value) =>
    `<span class="badge ${escapeHtml(String(value || "unknown").replaceAll(" ", "_"))}">${escapeHtml(value || "unknown")}</span>`;
  const setBusy = (key, busy) =>
    busy ? state.busy.add(key) : state.busy.delete(key);
  const focusableSelector =
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  function toast(message, level = "ok") {
    const el = document.createElement("div");
    el.className = `toast ${level === "ok" ? "" : level}`;
    el.textContent = message;
    toastRoot.append(el);
    window.setTimeout(() => el.remove(), 4600);
  }
  async function api(path, options = {}) {
    const body =
      options.body && typeof options.body !== "string"
        ? JSON.stringify(options.body)
        : options.body;
    const headers = {
      Accept: "application/json",
      ...(body ? { "Content-Type": "application/json" } : {}),
      ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}),
      ...(options.headers || {}),
    };
    let response;
    try {
      const { onUnauthorized, ...fetchOptions } = options;
      response = await fetch(path, { ...fetchOptions, body, headers });
    } catch {
      throw new Error(
        "Network unavailable. Confirm that the CloudAgent service is running.",
      );
    }
    if (response.status === 401) {
      if (options.onUnauthorized !== "throw" && state.token)
        disconnect("Access token expired or was rejected.");
      throw new Error("Authentication required");
    }
    const contentType = response.headers.get("content-type") || "";
    const data = contentType.includes("json")
      ? await response.json().catch(() => ({}))
      : await response.text();
    if (!response.ok)
      throw new Error(
        data?.error?.message ||
          data?.message ||
          `Request failed (${response.status})`,
      );
    return data;
  }
  function disconnect(
    message = "Disconnected. Token was removed from this browser session.",
  ) {
    state.loadEpoch += 1;
    state.token = "";
    state.overview = null;
    state.data = {};
    sessionStorage.removeItem(TOKEN_KEY);
    closeDialog({ restoreFocus: false });
    render();
    toast(message, "warn");
  }
  function gate() {
    return `<main id="main" class="gate"><section class="gate-card" data-testid="access-gate"><div class="eyebrow">CloudAgent / protected plane</div><h1>Access<br>Gate.</h1><p class="muted">This console talks only to the same-origin API. Your bearer token stays in <code>sessionStorage</code>, is never added to URLs, and is cleared when you disconnect or close this browser session.</p><form id="access-form" class="form-stack"><label class="field">Bearer token<input name="token" data-testid="access-token" type="password" autocomplete="current-password" required autofocus></label><button class="btn" data-testid="connect-button" type="submit">Enter control plane</button></form></section></main>`;
  }
  function shell(content) {
    const nav = views
      .map(
        (v, i) =>
          `<button type="button" data-view="${v}" ${state.view === v ? 'aria-current="page"' : ""}><span class="nav-index">0${i + 1}</span>${labels[v]}</button>`,
      )
      .join("");
    return `<div class="topbar"><div class="brand"><span class="brand-mark">CA</span><span>CloudAgent<small>CONTROL CONSOLE</small></span></div><button id="mobile-nav" class="btn secondary small" type="button" aria-controls="side-nav" aria-expanded="false">Menu</button></div><div class="shell"><aside class="sidebar" id="side-nav"><div class="brand"><span class="brand-mark">CA</span><span>CloudAgent<small>CONTROL CONSOLE</small></span></div><nav class="nav" aria-label="Workspace">${nav}</nav><div class="sidebar-footer"><span class="micro">Bearer session active</span><button class="btn secondary small" data-action="disconnect" type="button">Disconnect</button></div></aside><main id="main" class="workspace">${content}</main></div>`;
  }
  function heading(eyebrow, title, copy, actions = "") {
    return `<section class="view-heading"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p class="muted">${copy}</p></div>${actions && `<div class="button-row">${actions}</div>`}</section>`;
  }
  const empty = (title, copy, action = "") =>
    `<div class="empty"><strong>${escapeHtml(title)}</strong><p>${escapeHtml(copy)}</p>${action}</div>`;
  const panel = (title, body, action = "", cls = "") =>
    `<section class="panel ${cls}"><div class="panel-head"><h2>${escapeHtml(title)}</h2>${action}</div>${body}</section>`;
  function loading() {
    return `<div class="state"><span class="loading">Reading control plane</span></div>`;
  }
  function errorState(error) {
    return `<div class="state error"><strong>Unable to read this workspace</strong><p>${escapeHtml(error.message || error)}</p><button class="btn secondary small" type="button" data-action="refresh">Try again</button></div>`;
  }
  function overviewView() {
    if (!state.overview)
      return `<div class="view">${heading("CONTROL / 01", "Runtime overview", "Readiness, governance and current execution evidence.")}${loading()}</div>`;
    const o = state.overview,
      c = o.counts || {},
      rail = items(o.runtime_rail || o.rail),
      railById = Object.fromEntries(rail.map((entry) => [entry.id, entry]));
    const signals = Object.entries(o.signals || {}).map(([name, value]) => ({
      name: name.replaceAll("_", " "),
      message: `${name.replaceAll("_", " ")}: ${value}`,
      status: Number(value) > 0 ? "info" : "ready",
    }));
    const activity = items(o.activity);
    const node = (key, label, fallback) => {
      const railIds = {
        api: "api_gateway",
        policy: "policy",
        queue: "queue",
        worker: "worker",
        evidence: "artifact_audit",
      };
      const value = railById[railIds[key]] || fallback;
      const status =
        typeof value === "object"
          ? value.status || value.state || "unknown"
          : value;
      const detail =
        typeof value === "object"
          ? value.detail || value.count || value.summary || value.message || ""
          : "";
      return `<div class="rail-node ${key}"><span class="rail-dot ${status === "healthy" || status === "ready" || status === "active" ? "ok" : status === "failed" ? "bad" : "warn"}"></span><strong>${label}</strong><small>${escapeHtml(String(status))}${detail ? ` · ${escapeHtml(String(detail))}` : ""}</small></div>`;
    };
    const metric = (value, label, kind = "") =>
      `<section class="panel metric ${kind}"><strong>${escapeHtml(value ?? 0)}</strong><label>${escapeHtml(label)}</label></section>`;
    const recent =
      (o.recent_runs || [])
        .map(
          (r) =>
            `<li class="row"><div class="row-main"><strong>${id(r.id)}</strong><small>${escapeHtml(r.trigger_source || r.status || "run")}</small></div>${badge(r.status)}</li>`,
        )
        .join("") ||
      empty(
        "No runs yet",
        "Queue a job or send a session message to create the first governed run.",
      );
    const activityRows =
      (activity.length ? activity : signals)
        .slice(0, 7)
        .map(
          (a) =>
            `<div class="activity-item"><span class="rail-dot ${a.status === "failed" ? "bad" : a.status === "warning" ? "warn" : "ok"}"></span><span>${escapeHtml(a.message || a.name || a.action || a.type || "Control-plane signal")}</span><time class="activity-time">${when(a.created_at || a.at)}</time></div>`,
        )
        .join("") ||
      empty(
        "No activity signals",
        "Events will appear as work passes through the runtime rail.",
      );
    const bootstrap =
      !Number(c.agents || 0) ||
      !Number(c.sessions || 0) ||
      !Number(c.jobs || 0) ||
      !Number(c.workers || 0)
        ? `<button class="btn" data-action="bootstrap" type="button">Initialize showcase environment</button>`
        : "";
    return `<div class="view" data-testid="overview-view">${heading("CONTROL / 01", "Runtime overview", "Prove the governed path from API intake to auditable evidence.", `<button class="btn secondary" data-action="refresh" type="button">Refresh</button>${bootstrap}`)}<div class="grid"><div class="span-12">${panel("Runtime Rail", `<div class="rail" data-testid="runtime-rail">${node("api", "API", { status: "ready" })}${node("policy", "Policy", { status: "ready" })}${node("queue", "Queue", { status: c.job_runs || 0 ? "active" : "idle" })}${node("worker", "Worker", { status: c.workers || 0 ? "active" : "waiting" })}${node("evidence", "Evidence", { status: c.events || 0 ? "ready" : "empty" })}</div><p class="micro">Every transition is rendered from API data; run lease material is intentionally never displayed.</p>`)}</div>${metric(c.agents, "Agents")}${metric(c.sessions, "Sessions", "teal")}${metric(c.job_runs, "Runs", "amber")}${metric(c.pending_actions, "Pending approvals", "critical")}</div><div class="grid"><div class="span-7">${panel("Latest execution evidence", `<ul class="list">${recent}</ul>`, `<button class="btn secondary small" data-view="runtime" type="button">Open runtime</button>`)}</div><div class="span-5">${panel("Control signals", `<div class="activity">${activityRows}</div>`)}</div></div></div>`;
  }
  function optionRows(items, labelKey = "name") {
    return items
      .map(
        (x) =>
          `<option value="${escapeHtml(x.id)}">${escapeHtml(x[labelKey] || x.id)}</option>`,
      )
      .join("");
  }
  function runtimeView() {
    const d = state.data.runtime;
    if (!d)
      return `<div class="view">${heading("EXECUTION / 02", "Runtime", "Agents, environments, queues and workers.")}${loading()}</div>`;
    const table = (label, rows, columns, emptyCopy) =>
      rows.length
        ? `<div class="table-wrap"><table class="data-table" aria-label="${escapeHtml(label)}"><thead><tr>${columns.map((c) => `<th scope="col">${c[0]}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${columns.map((c) => `<td>${c[1](r)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`
        : empty("No records", emptyCopy);
    const processing = state.busy.has("process-next");
    const idleWorkerAvailable = d.workers.some(
      (worker) => worker.status === "active" && !worker.active_run_id,
    );
    const queuedRunAvailable = d.runs.some((run) => run.status === "queued");
    const queueHint = !idleWorkerAvailable
      ? "Register or reactivate an idle worker before processing the queue."
      : !queuedRunAvailable
        ? "Queue a job to make the next run available."
        : "Ready to claim and execute one queued run.";
    return `<div class="view" data-testid="runtime-view">${heading("EXECUTION / 02", "Runtime", "Create governed work, queue it, then process exactly one claimed run.", `<button class="btn secondary" data-action="refresh" type="button">Refresh</button>`)}<div class="grid"><div class="span-4">${panel(
      "Agents",
      table(
        "Agents",
        d.agents,
        [
          [
            "Name",
            (r) => `<strong>${escapeHtml(r.name)}</strong><br>${id(r.id)}`,
          ],
          ["Status", (r) => badge(r.status)],
        ],
        "Create an agent to define a runtime identity.",
      ),
      `<button class="btn small" data-modal="agent" type="button">Create agent</button>`,
    )}</div><div class="span-4">${panel(
      "Environments",
      table(
        "Environments",
        d.environments,
        [
          [
            "Name",
            (r) =>
              `<strong>${escapeHtml(r.name)}</strong><br><span class="micro">${escapeHtml(r.permission_profile_id || "—")}</span>`,
          ],
          ["Status", (r) => badge(r.status)],
        ],
        "Create an environment with an implemented policy profile.",
      ),
      `<button class="btn small" data-modal="environment" type="button">Create environment</button>`,
    )}</div><div class="span-4">${panel(
      "Workers",
      table(
        "Workers",
        d.workers,
        [
          [
            "Worker",
            (r) => `<strong>${escapeHtml(r.name)}</strong><br>${id(r.id)}`,
          ],
          ["State", (r) => badge(r.status)],
        ],
        "Register an active worker before processing a queue.",
      ),
      `<button class="btn small" data-modal="worker" type="button">Register worker</button>`,
    )}</div><div class="span-7">${panel(
      "Jobs",
      table(
        "Jobs",
        d.jobs,
        [
          [
            "Job",
            (r) => `<strong>${escapeHtml(r.name)}</strong><br>${id(r.id)}`,
          ],
          [
            "Trigger",
            (r) => escapeHtml(r.trigger_type || r.schedule?.type || "manual"),
          ],
          [
            "Action",
            (r) =>
              `<button class="btn secondary small" data-job-action="enqueue" data-id="${escapeHtml(r.id)}" type="button">Queue run</button>`,
          ],
        ],
        "Create a manual job after an agent and environment exist.",
      ),
      `<button class="btn small" data-modal="job" type="button">Create job</button>`,
    )}</div><div class="span-5">${panel("Queue control", `<p class="muted">Claims the next queued run through an active worker and executes it through the lease-scoped compatibility path. The returned lease token exists only in this function scope.</p><button class="btn" data-action="process-next" type="button" ${processing || !idleWorkerAvailable || !queuedRunAvailable ? "disabled" : ""}>${processing ? "Processing run…" : "Process next run"}</button><p class="micro">${queueHint}</p>`)}</div><div class="span-12">${panel(
      "Run ledger",
      table(
        "Run ledger",
        d.runs,
        [
          ["Run", (r) => id(r.id)],
          ["Source", (r) => escapeHtml(r.trigger_source || r.job_id || "—")],
          ["Worker", (r) => id(r.worker_id)],
          ["Status", (r) => badge(r.status)],
          ["Updated", (r) => when(r.updated_at)],
        ],
        "No runs. Queue a job or send a session message to inspect execution state.",
      ),
    )}</div></div></div>`;
  }
  function sessionsView() {
    const d = state.data.sessions;
    if (!d)
      return `<div class="view">${heading("COLLABORATION / 03", "Sessions", "Turns, approvals and evidence in one trace.")}${loading()}</div>`;
    const rows = d.sessions || [];
    return `<div class="view" data-testid="sessions-view">${heading("COLLABORATION / 03", "Sessions", "A session binds an agent, environment and optional Vault references.", `<button class="btn secondary" data-action="refresh" type="button">Refresh</button><button class="btn" data-modal="session" type="button" ${!d.agents.length || !d.environments.length ? "disabled" : ""}>Create session</button>`)}${!d.agents.length || !d.environments.length ? `<p class="secret-note">Create at least one agent and environment in Runtime before opening a session.</p>` : ""}${panel("Session register", rows.length ? `<div class="table-wrap"><table class="data-table" aria-label="Session register"><thead><tr><th scope="col">Session</th><th scope="col">Agent / environment</th><th scope="col">Turn</th><th scope="col">Last activity</th><th scope="col">Actions</th></tr></thead><tbody>${rows.map((s) => `<tr><td>${id(s.id)}</td><td class="mono">${escapeHtml(s.agent_snapshot?.id || "—")}<br>${escapeHtml(s.environment_snapshot?.id || "—")}</td><td>${badge(s.turn_status || s.status)}</td><td>${when(s.updated_at)}</td><td><button class="btn secondary small" data-session="${escapeHtml(s.id)}" type="button">Inspect</button></td></tr>`).join("")}</tbody></table></div>` : empty("No sessions", "Create a session to begin an auditable conversation."))}</div>`;
  }
  function governanceView() {
    const d = state.data.governance;
    if (!d)
      return `<div class="view">${heading("GOVERNANCE / 04", "Governance", "Profiles, tool policy, vault references.")}${loading()}</div>`;
    const cards = (items, emptyCopy) =>
      (items || []).length
        ? `<ul class="list">${items.map((p) => `<li><div class="row"><div class="row-main"><strong>${escapeHtml(p.name || p.display_name || p.id)}</strong><small>${escapeHtml(p.description || p.provider || p.runtime_mode || p.status || "")}</small></div>${badge(p.status || p.availability || "configured")}</div></li>`).join("")}</ul>`
        : empty("No records", emptyCopy);
    const tools = d.tools?.length
      ? `<ul class="list">${d.tools.map((tool) => `<li><div class="row"><div class="row-main"><strong>${escapeHtml(tool.name)}</strong><small>${escapeHtml(tool.description || tool.source || "")}</small></div><span>${badge(tool.executable ? tool.effective_policy?.decision || tool.default_policy : tool.status || "reference_only")} <span class="micro">${escapeHtml(tool.executable ? tool.effective_policy?.source || tool.source || "" : "capability boundary")}</span></span></div></li>`).join("")}</ul>`
      : empty("No built-in tools", "The runtime did not report Tool Gateway descriptors.");
    const policies = d.policies?.length
      ? `<ul class="list">${d.policies.map((policy) => `<li class="row"><div class="row-main"><strong>${escapeHtml(policy.scope)}</strong><small>${id(policy.id)}</small></div>${badge(policy.mode)}</li>`).join("")}</ul>`
      : empty(
          "No explicit policies",
          "Built-in and environment defaults remain in effect until an override is created.",
        );
    const vaults = d.vaults?.length
      ? `<ul class="list">${d.vaults.map((vault) => `<li><div class="row"><div class="row-main"><strong>${escapeHtml(vault.display_name || vault.id)}</strong><small>${escapeHtml(vault.credentials?.length || 0)} credential reference(s) · ${id(vault.id)}</small></div>${badge(vault.status)}</div></li>`).join("")}</ul>`
      : empty(
          "No Vaults",
          "Create a Vault to register redacted credential references.",
        );
    return `<div class="view" data-testid="governance-view">${heading("GOVERNANCE / 04", "Governance", "Only implemented controls are presented as executable. Reference-only items remain visibly constrained.", `<button class="btn secondary" data-action="refresh" type="button">Refresh</button>`)}<div class="grid"><div class="span-4">${panel("Permission profiles", cards(d.permissions, "The runtime did not report profiles."))}</div><div class="span-4">${panel("Sandbox profiles", cards(d.sandboxes, "The runtime did not report sandbox profiles."))}</div><div class="span-4">${panel("Tool gateway", tools)}</div><div class="span-7">${panel("Policy register", policies, `<button class="btn small" data-modal="policy" type="button">Create policy</button>`)}</div><div class="span-5">${panel("Vaults", vaults, `<button class="btn small" data-modal="vault" type="button">Create Vault</button>`)}</div><div class="span-12">${panel("Credential boundary", `<p class="secret-note">Credentials are write-only. After submission the secret field is immediately cleared. Stored credential references remain <strong>reference_only</strong>; runtime secret injection is currently disabled.</p>${d.vaults?.length ? `<button class="btn secondary small" data-modal="credential" type="button">Register credential</button>` : ""}`)}</div></div></div>`;
  }
  function resourcesView() {
    const d = state.data.resources;
    if (!d)
      return `<div class="view">${heading("EVIDENCE / 05", "Resources", "Files, artifacts and managed integration records.")}${loading()}</div>`;
    const files = d.files || [];
    const integrations = d.integrations || [];
    const artifacts = d.artifacts || [];
    return `<div class="view" data-testid="resources-view">${heading("EVIDENCE / 05", "Resources", "Persisted files and runtime outputs, with managed integration metadata.", `<button class="btn secondary" data-action="refresh" type="button">Refresh</button>`)}<div class="grid"><div class="span-6">${panel("Files", files.length ? `<ul class="list">${files.map((f) => `<li class="row"><div class="row-main"><strong>${escapeHtml(f.name)}</strong><small>${escapeHtml(f.content_type)} · ${escapeHtml(f.size)} bytes</small></div><button class="btn secondary small" type="button" data-file-download="${escapeHtml(f.id)}" data-download-path="/api/v1/files/${encodeURIComponent(f.id)}/content" data-file-name="${escapeHtml(f.name)}">Download</button></li>`).join("")}</ul>` : empty("No files", "Create a text or JSON file using the actual Files API."), `<button class="btn small" data-modal="file" type="button">Create file</button>`)}</div><div class="span-6">${panel("Integrations", integrations.length ? `<ul class="list">${integrations.map((i) => { const canRegister = Boolean(i.base_url); return `<li class="row"><div class="row-main"><strong>${escapeHtml(i.name)}</strong><small>${escapeHtml(i.provider)} · ${escapeHtml(i.base_url || "metadata only")}${canRegister ? "" : " · Add a base URL before credential registration"}</small></div><span class="row-actions">${badge(i.credential_status || i.status)}<button class="btn secondary small" data-modal="integration-credential" data-integration-id="${escapeHtml(i.id)}" type="button" ${canRegister ? "" : "disabled"}>${i.credential_status === "registered" ? "Re-register credential" : "Register credential"}</button></span></li>`; }).join("")}</ul><p class="micro">Credentials remain only in this server process. After a restart, register them again; values are never displayed.</p>` : empty("No integrations", "Create a managed connector record, then register its write-only credential."), `<button class="btn small" data-modal="integration" type="button">Create integration</button>`)}</div><div class="span-12">${panel("Session artifacts", artifacts.length ? `<div class="table-wrap"><table class="data-table" aria-label="Session artifacts"><thead><tr><th scope="col">Artifact</th><th scope="col">Session</th><th scope="col">Type</th><th scope="col">Created</th><th scope="col">Actions</th></tr></thead><tbody>${artifacts.map((a) => `<tr><td>${escapeHtml(a.name || a.id)}<br>${id(a.id)}</td><td>${id(a.session_id)}</td><td>${escapeHtml(a.content_type || "—")}</td><td>${when(a.created_at)}</td><td><button class="btn secondary small" type="button" data-artifact-download="${escapeHtml(a.id)}" data-download-path="/api/v1/artifacts/${encodeURIComponent(a.id)}/content" data-artifact-name="${escapeHtml(a.name || a.id)}">Download</button></td></tr>`).join("")}</tbody></table></div>` : empty("No artifacts", "Artifacts appear after a worker executes a session run."))}</div></div></div>`;
  }
  function render() {
    app.innerHTML = !state.token
      ? gate()
      : shell(
          {
            overview: overviewView,
            runtime: runtimeView,
            sessions: sessionsView,
            governance: governanceView,
            resources: resourcesView,
          }[state.view](),
        );
    bind();
  }
  async function loadView() {
    if (!state.token) return;
    const v = state.view;
    const token = state.token;
    const epoch = ++state.loadEpoch;
    try {
      if (v === "overview")
        state.overview = await api("/api/v1/admin/overview");
      if (v === "runtime") {
        const [
          agents,
          environments,
          jobs,
          workers,
          runs,
          permissions,
          sandboxes,
        ] = await Promise.all(
          [
            "agents",
            "environments",
            "jobs",
            "workers",
            "runs",
            "permission-profiles",
            "sandbox-profiles",
          ].map((resource) => api(`/api/v1/${resource}`)),
        );
        state.data.runtime = {
          agents: listData(agents),
          environments: listData(environments),
          jobs: listData(jobs),
          workers: listData(workers),
          runs: listData(runs),
          permissions: listData(permissions),
          sandboxes: listData(sandboxes),
        };
      }
      if (v === "sessions") {
        const [sessions, agents, environments, vaults, tools] = await Promise.all(
          ["sessions", "agents", "environments", "vaults", "tools"].map((resource) =>
            api(`/api/v1/${resource}`),
          ),
        );
        state.data.sessions = {
          sessions: listData(sessions),
          agents: listData(agents),
          environments: listData(environments),
          vaults: listData(vaults),
          tools: listData(tools),
        };
      }
      if (v === "governance") {
        const [permissions, sandboxes, tools, policies, vaults] =
          await Promise.all(
            [
              "permission-profiles",
              "sandbox-profiles",
              "tools",
              "tool-policies",
              "vaults",
            ].map((resource) => api(`/api/v1/${resource}`)),
          );
        state.data.governance = {
          permissions: listData(permissions),
          sandboxes: listData(sandboxes),
          tools: listData(tools),
          policies: listData(policies),
          vaults: listData(vaults),
        };
      }
      if (v === "resources") {
        const [files, integrations, artifacts] = await Promise.all([
          api("/api/v1/files"),
          api("/api/v1/integrations"),
          api("/api/v1/artifacts"),
        ]);
        state.data.resources = {
          files: listData(files),
          integrations: listData(integrations),
          artifacts: listData(artifacts),
        };
      }
      if (epoch !== state.loadEpoch || token !== state.token) return;
      render();
    } catch (error) {
      if (epoch !== state.loadEpoch || token !== state.token || !state.token)
        return;
      const content = `<div class="view">${heading("CONTROL PLANE", labels[v], "Live data could not be loaded.")}${errorState(error)}</div>`;
      app.innerHTML = shell(content);
      bind();
    }
  }
  function setBackgroundInert(inert) {
    [app, document.querySelector(".skip-link")].filter(Boolean).forEach((node) => {
      if (inert) {
        node.setAttribute("inert", "");
        node.setAttribute("aria-hidden", "true");
      } else {
        node.removeAttribute("inert");
        node.removeAttribute("aria-hidden");
      }
    });
    document.body.classList.toggle("dialog-open", inert);
  }
  function focusDialog() {
    const dialog = dialogRoot.querySelector('[role="dialog"]');
    const target =
      (dialogFocusSelector && dialog?.querySelector(dialogFocusSelector)) ||
      dialog?.querySelector("[autofocus]") ||
      dialog?.querySelector("input, select, textarea") ||
      dialog?.querySelector("button");
    requestAnimationFrame(() => target?.focus());
  }
  function renderDialog(markup, focusSelector = null) {
    dialogFocusSelector = focusSelector;
    dialogRoot.innerHTML = markup;
    setBackgroundInert(true);
    bindDialog();
    focusDialog();
  }
  function modal(title, body, focusSelector = null) {
    renderDialog(
      `<div class="modal-backdrop" role="presentation"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="dialog-title"><div class="dialog-head"><h2 id="dialog-title" tabindex="-1">${escapeHtml(title)}</h2><button class="icon-btn" data-action="close-dialog" aria-label="Close dialog" type="button">×</button></div>${body}</section></div>`,
      focusSelector,
    );
  }
  function closeDialog({ restoreFocus = true } = {}) {
    dialogEpoch += 1;
    const returnFocus = dialogReturnFocus;
    dialogRoot.innerHTML = "";
    dialogFocusSelector = null;
    setBackgroundInert(false);
    dialogReturnFocus = null;
    if (restoreFocus && returnFocus?.isConnected) returnFocus.focus();
  }
  function dialogError(form, message) {
    const error = form.querySelector("[data-dialog-error]");
    if (!error) return;
    error.textContent = message;
    error.hidden = false;
  }
  function clearDialogError(form) {
    const error = form.querySelector("[data-dialog-error]");
    if (!error) return;
    error.textContent = "";
    error.hidden = true;
  }
  function formModal(kind, context = {}) {
    const d = state.data[state.view] || {};
    const select = (name, label, items, labelKey = "name") =>
      `<label class="field">${label}<select name="${name}" required><option value="">Select…</option>${optionRows(items, labelKey)}</select></label>`;
    let title, form;
    let submitLabel = "Create";
    if (kind === "agent") {
      title = "Create agent";
      form = `<label class="field">Name<input name="name" required></label><label class="field">Description<input name="description"></label><label class="field">System instruction<textarea name="system"></textarea></label>`;
    } else if (kind === "environment") {
      const valid = (d.permissions || []).filter(
        (profile) => profile.status !== "blocked",
      );
      const implementedSandboxes = (d.sandboxes || []).filter(
        (profile) => profile.status === "implemented",
      );
      title = "Create environment";
      form = `<label class="field">Name<input name="name" required></label><label class="field">Permission profile<select name="permission_profile_id">${optionRows(valid)}</select></label><label class="field">Sandbox profile<select name="sandbox_profile_id">${optionRows(implementedSandboxes)}</select></label><p class="micro">Only implemented provider/profile combinations can be created.</p>`;
    } else if (kind === "worker") {
      title = "Register worker";
      form = `<label class="field">Worker ID<input name="id" placeholder="worker_console_1" required></label><label class="field">Display name<input name="name" required></label>`;
    } else if (kind === "job") {
      title = "Create job";
      form = `<label class="field">Name<input name="name" required></label>${select("agent_id", "Agent", d.agents || [])}${select("environment_id", "Environment", d.environments || [])}<label class="field">Trigger<select name="trigger"><option value="manual">Manual</option><option value="delay">Delay</option></select></label><label class="field" data-delay-field hidden>Delay seconds<input name="delay_seconds" type="number" min="1" max="86400" value="30"></label>`;
    } else if (kind === "session") {
      title = "Create session";
      form = `${select("agent_id", "Agent", d.agents || [])}${select("environment_id", "Environment", d.environments || [])}<label class="field">Vault references<select name="vault_ids" multiple>${optionRows(d.vaults || [], "display_name")}</select></label><p class="micro">Optional Vault IDs are bound when the session is created. Credential values remain write-only and are not injected at runtime.</p>`;
    } else if (kind === "policy") {
      title = "Create tool policy";
      form = `<label class="field">Scope<input name="scope" placeholder="integration.dify.chat or *" required></label><label class="field">Mode<select name="mode"><option value="always_ask">always_ask</option><option value="always_allow">always_allow</option><option value="always_deny">always_deny</option></select></label><p class="micro">Policies can restrict implemented tools. Reference-only tools remain fail-closed and cannot be enabled.</p>`;
    } else if (kind === "vault") {
      title = "Create Vault";
      form = `<label class="field">Display name<input name="display_name" required></label>`;
    } else if (kind === "credential") {
      title = "Register write-only credential";
      submitLabel = "Register credential";
      form = `${select("vault_id", "Vault", d.vaults || [], "display_name")}<label class="field">Type<select name="type"><option value="static_bearer">static_bearer</option><option value="environment_variable">environment_variable</option></select></label><label class="field" data-credential-name hidden>Variable name<input name="name" placeholder="SERVICE_TOKEN"></label><label class="field">Secret<input name="secret" type="password" autocomplete="off" required></label><p class="secret-note">After create, this field is cleared. The server returns only a redacted reference; injection is still disabled.</p>`;
    } else if (kind === "file") {
      title = "Create file";
      form = `<label class="field">Name<input name="name" required></label><label class="field">Content type<select name="content_type"><option value="text/plain; charset=utf-8">Plain text</option><option value="application/json">JSON</option></select></label><label class="field">Content<textarea name="content"></textarea></label>`;
    } else if (kind === "integration") {
      title = "Create integration";
      form = `<label class="field">Provider<select name="provider"><option value="feishu">Feishu</option><option value="dify">Dify</option><option value="github_actions">GitHub Actions</option><option value="webhook">Webhook</option></select></label><label class="field">Name<input name="name" required></label><label class="field">Base URL<input name="base_url" type="url"></label><p class="micro">Create the connector record first. Its credential is registered separately as write-only material and is never returned to the browser.</p>`;
    } else if (kind === "integration-credential") {
      title = "Register integration credential";
      submitLabel = "Register credential";
      const selectedId = context.integrationId || "";
      form = `<label class="field">Integration<select name="integration_id" required><option value="">Select…</option>${(d.integrations || []).map((integration) => `<option value="${escapeHtml(integration.id)}" ${integration.id === selectedId ? "selected" : ""}>${escapeHtml(integration.name || integration.id)}</option>`).join("")}</select></label><label class="field">Credential<input name="secret" type="password" autocomplete="off" required></label><p class="secret-note">This action replaces the currently registered credential. The value is sent once, immediately cleared, and never displayed again.</p>`;
    } else return;
    modal(
      title,
      `<form id="resource-form" data-kind="${kind}" class="form-stack" novalidate><div class="dialog-error" data-dialog-error role="alert" hidden></div>${form}<div class="button-row"><button class="btn secondary" data-action="close-dialog" type="button">Cancel</button><button class="btn" type="submit">${escapeHtml(submitLabel)}</button></div></form>`,
    );
  }
  async function submitModal(form) {
    if (!form.checkValidity()) {
      form.reportValidity();
      return;
    }
    clearDialogError(form);
    const kind = form.dataset.kind,
      fd = new FormData(form);
    const value = (key) => String(fd.get(key) || "").trim();
    let path,
      body = {};
    if (kind === "agent") {
      path = "/api/v1/agents";
      body = {
        name: value("name"),
        description: value("description"),
        system: value("system"),
      };
    }
    if (kind === "environment") {
      path = "/api/v1/environments";
      body = {
        name: value("name"),
        permission_profile_id: value("permission_profile_id"),
        sandbox_profile_id: value("sandbox_profile_id"),
      };
    }
    if (kind === "worker") {
      path = "/api/v1/workers";
      body = { id: value("id"), name: value("name") };
    }
    if (kind === "job") {
      const triggerType = value("trigger");
      path = "/api/v1/jobs";
      body = {
        name: value("name"),
        agent_id: value("agent_id"),
        environment_id: value("environment_id"),
        trigger:
          triggerType === "delay"
            ? {
                type: "delay",
                delay_seconds: Number(value("delay_seconds")),
              }
            : { type: "manual" },
      };
    }
    if (kind === "session") {
      path = "/api/v1/sessions";
      body = {
        agent_id: value("agent_id"),
        environment_id: value("environment_id"),
        vault_ids: fd
          .getAll("vault_ids")
          .map((vaultId) => String(vaultId).trim())
          .filter(Boolean),
      };
    }
    if (kind === "policy") {
      path = "/api/v1/tool-policies";
      body = { scope: value("scope"), mode: value("mode") };
    }
    if (kind === "vault") {
      path = "/api/v1/vaults";
      body = { display_name: value("display_name") };
    }
    if (kind === "credential") {
      path = `/api/v1/vaults/${encodeURIComponent(value("vault_id"))}/credentials`;
      body = {
        auth:
          value("type") === "environment_variable"
            ? {
                type: "environment_variable",
                name: value("name"),
                value: value("secret"),
              }
            : { type: "static_bearer", token: value("secret") },
      };
    }
    if (kind === "file") {
      path = "/api/v1/files";
      const rawContent = String(fd.get("content") || "");
      const contentType = value("content_type");
      let content = rawContent;
      if (contentType === "application/json") {
        try {
          content = JSON.stringify(JSON.parse(rawContent), null, 2);
        } catch {
          dialogError(form, "JSON content is not valid.");
          toast("JSON content is not valid.", "error");
          form.querySelector("textarea[name=content]")?.focus();
          return;
        }
      }
      body = {
        name: value("name"),
        content_type: contentType,
        content,
      };
    }
    if (kind === "integration") {
      path = "/api/v1/integrations";
      body = {
        provider: value("provider"),
        name: value("name"),
        base_url: value("base_url") || undefined,
      };
    }
    if (kind === "integration-credential") {
      path = `/api/v1/integrations/${encodeURIComponent(value("integration_id"))}/credential`;
      body = { secret: value("secret") };
    }
    const submit = form.querySelector("button[type=submit]");
    submit.disabled = true;
    try {
      await api(path, { method: "POST", body });
      form
        .querySelectorAll("input[type=password],textarea[name=secret]")
        .forEach((x) => (x.value = ""));
      closeDialog();
      toast(
        kind === "integration-credential"
          ? "Integration credential registered."
          : kind === "integration"
            ? "Integration created. Register its credential before use."
            : `${labels[state.view]} record created.`,
      );
      await loadView();
      document.querySelector(`[data-modal="${kind}"]`)?.focus();
    } catch (error) {
      dialogError(form, error.message || "Unable to save this record.");
      toast(error.message, "error");
      submit.disabled = false;
    }
  }
  async function processNext() {
    const d = state.data.runtime;
    const worker = d?.workers.find(
      (w) => w.status === "active" && !w.active_run_id,
    );
    if (!worker) {
      toast("No active, idle worker is available.", "warn");
      return;
    }
    setBusy("process-next", true);
    render();
    try {
      const claim = await api(
        `/api/v1/workers/${encodeURIComponent(worker.id)}/claim`,
        { method: "POST", body: {} },
      );
      const run = claim.run;
      if (!run?.id || !run.lease_token) {
        toast("No queued run is available to claim.", "warn");
        return;
      }
      const leaseToken = run.lease_token;
      const runBase = `/api/v1/workers/${encodeURIComponent(worker.id)}/runs/${encodeURIComponent(run.id)}`;
      if (String(run.trigger_source || "").startsWith("tool:")) {
        const pending = listData(
          await api(
            `/api/v1/sessions/${encodeURIComponent(run.session_id)}/pending-actions`,
          ),
        );
        const action = pending.find(
          (item) =>
            item.status === "approved" && item.execution_run_id === run.id,
        );
        if (!action) {
          throw new Error("No approved tool action is assigned to this queued run.");
        }
        let toolExecution;
        try {
          toolExecution = await api(`${runBase}/tools/execute`, {
            method: "POST",
            body: { lease_token: leaseToken, action_id: action.id },
          });
          await api(`${runBase}/complete`, {
            method: "POST",
            body: {
              lease_token: leaseToken,
              status: "succeeded",
              result: { tool_execution: toolExecution.result || {} },
            },
          });
        } catch (error) {
          try {
            await api(`${runBase}/complete`, {
              method: "POST",
              body: {
                lease_token: leaseToken,
                status: "failed",
                result: { error: error.message || "Tool execution failed" },
              },
            });
          } catch {
            // Preserve the primary tool-execution error for the operator.
          }
          throw error;
        }
        toast(`Approved tool ${action.tool} executed through ${worker.name}.`);
      } else {
        await api(`${runBase}/execute`, {
          method: "POST",
          body: { lease_token: leaseToken },
        });
        toast(`Run ${run.id} executed through ${worker.name}.`);
      }
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setBusy("process-next", false);
      await loadView();
    }
  }
  async function inspectSession(sessionId) {
    const epoch = ++dialogEpoch;
    modal(
      "Session evidence",
      `<div class="state"><span class="loading">Loading session trace</span></div>`,
      "#dialog-title",
    );
    try {
      const base = `/api/v1/sessions/${encodeURIComponent(sessionId)}`;
      const [session, events, artifacts, usage, pending, audit] =
        await Promise.all([
          api(base),
          api(`${base}/events`),
          api(`${base}/artifacts`),
          api(`${base}/usage`),
          api(`${base}/pending-actions`),
          api(`${base}/audit`),
        ]);
      if (epoch !== dialogEpoch || !state.token) return;
      const E = listData(events),
        A = listData(artifacts),
        U = listData(usage),
        P = listData(pending),
        X = listData(audit);
      const executableTools = (state.data.sessions?.tools || []).filter(
        (tool) => tool.executable,
      );
      const usageSummary = U.reduce(
        (total, record) => ({
          tokenInput: total.tokenInput + Number(record.token_input || 0),
          tokenOutput: total.tokenOutput + Number(record.token_output || 0),
          duration: total.duration + Number(record.tool_duration_ms || 0),
          cpu: total.cpu + Number(record.sandbox_cpu_ms || 0),
          memory: Math.max(total.memory, Number(record.sandbox_memory_peak_mb || 0)),
        }),
        { tokenInput: 0, tokenOutput: 0, duration: 0, cpu: 0, memory: 0 },
      );
      const timeline = E.length
        ? `<div class="activity">${E.map((e) => `<div class="activity-item"><span class="rail-dot ${e.severity === "error" ? "bad" : "ok"}"></span><span class="event-main"><strong>${escapeHtml(e.type)}</strong><br>${payloadView(e.payload)}</span><time class="activity-time">${when(e.created_at)}</time></div>`).join("")}</div>`
        : empty(
            "No timeline entries",
            "Events will be recorded after a user message or worker turn.",
          );
      const actions = P.length
        ? `<ul class="list">${P.map((a) => `<li class="action-card"><div class="row"><div class="row-main"><strong>${escapeHtml(a.tool || a.id)}</strong><small>${escapeHtml(a.status)} · ${when(a.created_at)}</small></div>${badge(a.status)}</div><details class="event-details"><summary>Proposed arguments</summary><pre>${escapeHtml(JSON.stringify(a.proposed_args || {}, null, 2))}</pre></details>${a.reason ? `<p class="micro">Resolution reason: ${escapeHtml(a.reason)}</p>` : ""}${a.status === "pending" ? `<div class="approval-controls"><label class="field">Decision reason (optional)<input data-resolution-reason data-action-id="${escapeHtml(a.id)}" maxlength="500"></label><span class="button-row"><button class="btn small" data-resolve="approve" data-session-id="${escapeHtml(sessionId)}" data-action-id="${escapeHtml(a.id)}" type="button">Approve</button><button class="btn danger small" data-resolve="reject" data-session-id="${escapeHtml(sessionId)}" data-action-id="${escapeHtml(a.id)}" type="button">Reject</button></span></div>` : ""}</li>`).join("")}</ul>`
        : empty(
            "No pending actions",
            "Approval-required tool requests will appear here.",
          );
      const toolOptions = executableTools
        .map(
          (tool) =>
            `<option value="${escapeHtml(tool.name)}">${escapeHtml(tool.name)} (${escapeHtml(tool.effective_policy?.mode || tool.default_policy || "policy" )})</option>`,
        )
        .join("");
      const auditTrail = X.length
        ? `<ul class="list">${X.map((record) => `<li><details class="event-details"><summary>${escapeHtml(record.action || record.id)} · ${when(record.created_at)}</summary><pre>${escapeHtml(JSON.stringify({ actor: record.actor, target_type: record.target_type, target_id: record.target_id, request_id: record.request_id }, null, 2))}</pre></details></li>`).join("")}</ul>`
        : empty("No audit records", "Control-plane actions will be recorded here.");
      renderDialog(
        `<div class="drawer-backdrop" role="presentation"><aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="session-dialog-title" data-testid="session-drawer" data-session-id="${escapeHtml(session.id)}"><div class="dialog-head"><div><div class="eyebrow">SESSION TRACE</div><h2 id="session-dialog-title" tabindex="-1">${id(session.id)}</h2></div><button class="icon-btn" data-action="close-dialog" aria-label="Close session details" type="button">×</button></div><p>${badge(session.turn_status || session.status)} <span class="micro">Agent ${escapeHtml(session.agent_snapshot?.id || "—")} · Environment ${escapeHtml(session.environment_snapshot?.id || "—")}</span></p><section class="panel"><h3>Send user message</h3><form id="message-form" class="form-stack" novalidate><div class="dialog-error" data-dialog-error role="alert" hidden></div><label class="field">Message<textarea name="message" required></textarea></label><button class="btn" type="submit">Queue governed turn</button></form></section><section class="panel"><h3>Request governed tool</h3>${executableTools.length ? `<form id="tool-form" class="form-stack" novalidate><div class="dialog-error" data-dialog-error role="alert" hidden></div><label class="field">Tool<select name="tool" required><option value="">Select an implemented tool…</option>${toolOptions}</select></label><p class="micro">The selected tool follows its effective policy. Use Governance to set an implemented tool to <code>always_ask</code> when demonstrating approvals.</p><label class="field">Arguments (JSON object)<textarea name="args" required>{}</textarea></label><button class="btn secondary" type="submit">Submit tool request</button></form>` : `<p class="micro">No executable tools are available for this session.</p>`}</section><section class="panel"><h3>Timeline</h3>${timeline}</section><section class="panel"><h3>Pending actions</h3>${actions}</section><section class="panel"><h3>Evidence & usage</h3><p class="micro">${A.length} artifact(s), ${U.length} usage record(s), ${X.length} audit record(s).</p><dl class="usage-summary"><div><dt>Input tokens</dt><dd>${escapeHtml(usageSummary.tokenInput)}</dd></div><div><dt>Output tokens</dt><dd>${escapeHtml(usageSummary.tokenOutput)}</dd></div><div><dt>Tool duration</dt><dd>${escapeHtml(usageSummary.duration)} ms</dd></div><div><dt>Sandbox CPU</dt><dd>${escapeHtml(usageSummary.cpu)} ms</dd></div><div><dt>Peak memory</dt><dd>${escapeHtml(usageSummary.memory)} MB</dd></div></dl>${A.length ? `<ul class="list">${A.map((a) => `<li class="row"><div class="row-main"><strong>${escapeHtml(a.name || a.id)}</strong><small>${escapeHtml(a.content_type || "artifact")} · ${escapeHtml(a.size || 0)} bytes</small></div><button class="btn secondary small" type="button" data-artifact-download="${escapeHtml(a.id)}" data-download-path="/api/v1/artifacts/${encodeURIComponent(a.id)}/content" data-artifact-name="${escapeHtml(a.name || a.id)}">Download</button></li>`).join("")}</ul>` : empty("No artifacts", "Worker-created artifacts will appear here.")}</section><section class="panel"><h3>Audit trail</h3>${auditTrail}</section></aside></div>`,
        "#session-dialog-title",
      );
    } catch (error) {
      toast(error.message, "error");
      closeDialog();
    }
  }
  async function resolveAction(btn) {
    btn.disabled = true;
    try {
      const reason = [...dialogRoot.querySelectorAll("[data-resolution-reason]")].find(
        (input) => input.dataset.actionId === btn.dataset.actionId,
      )?.value;
      await api(
        `/api/v1/sessions/${encodeURIComponent(btn.dataset.sessionId)}/pending-actions/${encodeURIComponent(btn.dataset.actionId)}/resolve`,
        { method: "POST", body: { decision: btn.dataset.resolve, reason: String(reason || "").trim() } },
      );
      toast(btn.dataset.resolve === "approve" ? "Action approved." : "Action rejected.");
      inspectSession(btn.dataset.sessionId);
    } catch (error) {
      toast(error.message, "error");
      btn.disabled = false;
    }
  }
  async function sendMessage(form) {
    const sessionId = dialogRoot.querySelector(
      "[data-testid=session-drawer]",
    )?.dataset.sessionId;
    const text = new FormData(form).get("message");
    if (!sessionId || !String(text).trim()) {
      dialogError(form, "Message is required.");
      form.querySelector("textarea[name=message]")?.focus();
      return;
    }
    clearDialogError(form);
    const submit = form.querySelector("button[type=submit]");
    submit.disabled = true;
    try {
      await api(`/api/v1/sessions/${encodeURIComponent(sessionId)}/events`, {
        method: "POST",
        body: { type: "user.message", payload: { text: String(text) } },
      });
      form.reset();
      toast("User message accepted and a governed turn was queued.");
      inspectSession(sessionId);
    } catch (error) {
      dialogError(form, error.message || "Unable to queue the message.");
      toast(error.message, "error");
      submit.disabled = false;
    }
  }
  async function requestTool(form) {
    const sessionId = dialogRoot.querySelector(
      "[data-testid=session-drawer]",
    )?.dataset.sessionId;
    if (!sessionId || !form.checkValidity()) {
      form.reportValidity();
      return;
    }
    clearDialogError(form);
    const submit = form.querySelector("button[type=submit]");
    let args;
    try {
      args = JSON.parse(String(new FormData(form).get("args") || "{}"));
      if (!args || Array.isArray(args) || typeof args !== "object")
        throw new Error("Arguments must be a JSON object.");
    } catch (error) {
      dialogError(form, error.message || "Arguments must be valid JSON.");
      return;
    }
    submit.disabled = true;
    try {
      await api(`/api/v1/sessions/${encodeURIComponent(sessionId)}/events`, {
        method: "POST",
        body: {
          type: "tool.requested",
          payload: { tool: new FormData(form).get("tool"), args },
        },
      });
      toast("Tool request recorded for policy evaluation.");
      inspectSession(sessionId);
    } catch (error) {
      dialogError(form, error.message || "Unable to request the tool.");
      toast(error.message, "error");
      submit.disabled = false;
    }
  }
  function bindCopyButtons(root) {
    root.querySelectorAll("[data-copy]").forEach((button) =>
      button.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(button.dataset.copy);
          toast("Identifier copied.");
        } catch {
          toast("Clipboard access is unavailable.", "warn");
        }
      }),
    );
  }
  function bindDownload(link) {
    link.addEventListener("click", async (event) => {
      event.preventDefault();
      const filename =
        link.dataset.fileName || link.dataset.artifactName || "cloudagent-download";
      try {
        const res = await fetch(link.dataset.downloadPath, {
          headers: { Authorization: `Bearer ${state.token}` },
        });
        if (res.status === 401) {
          disconnect("Access token expired or was rejected.");
          return;
        }
        if (!res.ok) throw new Error(`Download failed (${res.status})`);
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const download = document.createElement("a");
        download.href = url;
        download.download = String(filename).replace(/[\\/:*?"<>|]/g, "_");
        document.body.append(download);
        download.click();
        download.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 0);
      } catch (error) {
        toast(error.message || "Download failed.", "error");
      }
    });
  }
  function bindDialog() {
    dialogRoot
      .querySelectorAll("[data-action=close-dialog]")
      .forEach((button) => button.addEventListener("click", closeDialog));
    dialogRoot.querySelector("#resource-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      submitModal(event.currentTarget);
    });
    dialogRoot
      .querySelector("#resource-form [name=type]")
      ?.addEventListener("change", (event) => {
        const field = dialogRoot.querySelector("[data-credential-name]");
        const input = field?.querySelector("input");
        const requiresName = event.target.value === "environment_variable";
        if (field) field.hidden = !requiresName;
        if (input) input.required = requiresName;
      });
    dialogRoot
      .querySelector("#resource-form [name=trigger]")
      ?.addEventListener("change", (event) => {
        const field = dialogRoot.querySelector("[data-delay-field]");
        const input = field?.querySelector("input");
        const delayed = event.target.value === "delay";
        if (field) field.hidden = !delayed;
        if (input) input.required = delayed;
      });
    dialogRoot.querySelector("#message-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      sendMessage(event.currentTarget);
    });
    dialogRoot.querySelector("#tool-form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      requestTool(event.currentTarget);
    });
    dialogRoot
      .querySelectorAll("[data-resolve]")
      .forEach((button) =>
        button.addEventListener("click", () => resolveAction(button)),
      );
    dialogRoot
      .querySelectorAll(".modal-backdrop, .drawer-backdrop")
      .forEach((backdrop) =>
        backdrop.addEventListener("click", (event) => {
          if (event.target === backdrop) closeDialog();
        }),
      );
    bindCopyButtons(dialogRoot);
    dialogRoot.querySelectorAll("[data-artifact-download]").forEach(bindDownload);
  }
  function bind() {
    document
      .querySelector("#access-form")
      ?.addEventListener("submit", async (e) => {
        e.preventDefault();
        const input = e.currentTarget.elements.token;
        const submit = e.currentTarget.querySelector("button");
        submit.disabled = true;
        state.token = input.value;
        try {
          await api("/api/v1/admin/overview", { onUnauthorized: "throw" });
          sessionStorage.setItem(TOKEN_KEY, state.token);
          state.overview = null;
          render();
          loadView();
        } catch (error) {
          state.token = "";
          input.value = "";
          toast(error.message, "error");
          submit.disabled = false;
        }
      });
    document.querySelectorAll("[data-view]").forEach((b) =>
      b.addEventListener("click", () => {
        state.view = b.dataset.view;
        sessionStorage.setItem(VIEW_KEY, state.view);
        document.querySelector(".sidebar")?.classList.remove("open");
        document.querySelector("#mobile-nav")?.setAttribute("aria-expanded", "false");
        render();
        loadView();
      }),
    );
    document.querySelector("#mobile-nav")?.addEventListener("click", (e) => {
      const nav = document.querySelector(".sidebar");
      nav.classList.toggle("open");
      e.currentTarget.setAttribute(
        "aria-expanded",
        String(nav.classList.contains("open")),
      );
    });
    document
      .querySelectorAll("[data-action=disconnect]")
      .forEach((b) => b.addEventListener("click", () => disconnect()));
    document
      .querySelectorAll("[data-action=refresh]")
      .forEach((b) => b.addEventListener("click", () => loadView()));
    document
      .querySelectorAll("[data-modal]")
      .forEach((b) =>
        b.addEventListener("click", () => {
          dialogReturnFocus = b;
          formModal(b.dataset.modal, { integrationId: b.dataset.integrationId });
        }),
      );
    document.querySelectorAll("[data-job-action]").forEach((b) =>
      b.addEventListener("click", async () => {
        b.disabled = true;
        try {
          await api(
            `/api/v1/jobs/${encodeURIComponent(b.dataset.id)}/${b.dataset.jobAction}`,
            { method: "POST", body: {} },
          );
          toast("Job run queued.");
          await loadView();
        } catch (error) {
          toast(error.message, "error");
          b.disabled = false;
        }
      }),
    );
    document
      .querySelector("[data-action=process-next]")
      ?.addEventListener("click", processNext);
    document
      .querySelector("[data-action=bootstrap]")
      ?.addEventListener("click", async (e) => {
        e.currentTarget.disabled = true;
        try {
          await api("/api/v1/admin/showcase/bootstrap", {
            method: "POST",
            body: {},
          });
          toast("Showcase environment initialized.");
          await loadView();
        } catch (error) {
          toast(error.message, "error");
          e.currentTarget.disabled = false;
        }
      });
    document
      .querySelectorAll("[data-session]")
      .forEach((b) =>
        b.addEventListener("click", () => {
          dialogReturnFocus = b;
          inspectSession(b.dataset.session);
        }),
      );
    bindCopyButtons(app);
    document.querySelectorAll("[data-file-download], [data-artifact-download]").forEach(bindDownload);
  }
  document.addEventListener("keydown", (event) => {
    if (!dialogRoot.childElementCount) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeDialog();
      return;
    }
    if (event.key !== "Tab") return;
    const dialog = dialogRoot.querySelector('[role="dialog"]');
    const controls = [...(dialog?.querySelectorAll(focusableSelector) || [])].filter(
      (element) => !element.hidden && element.offsetParent !== null,
    );
    if (!controls.length) {
      event.preventDefault();
      dialog?.focus();
      return;
    }
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  if (!views.includes(state.view)) state.view = "overview";
  render();
  if (state.token) loadView();
})();
