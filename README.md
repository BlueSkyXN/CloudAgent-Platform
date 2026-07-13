# CloudAgent-Platform

CloudAgent-Platform is being built as an API-first cloud Agent cluster platform:
programmable scheduling, long-running session/event handling, external
integrations, and an admin control surface.

Current repository state:

- `src/cloudagent_platform/` contains a dependency-free local runtime prototype with P0.5 module boundaries for config, utilities, OpenAPI, scheduler, HTTP routing, tool registry, and connector metadata.
- `cloud/hfs/` contains the Hugging Face Docker Space deployment wrapper.
- `local/20260616/` contains the SDLC package and target OpenAPI draft; the implemented local contract is served by `/openapi.json` from code.

## Local Prototype

Run the standard-library service:

```bash
PYTHONPATH=src CLOUDAGENT_AUTH_TOKEN=dev-local-token python3 -m cloudagent_platform --port 8080
```

Run one external HTTP worker pass against the local service:

```bash
PYTHONPATH=src cloudagent-worker \
  --base-url http://127.0.0.1:8080 \
  --token dev-local-token \
  --worker-id worker_local_http \
  --once
```

Then open:

- Admin panel: <http://127.0.0.1:8080/admin>
- Health: <http://127.0.0.1:8080/_ops/healthz>
- Readiness: <http://127.0.0.1:8080/_ops/readyz>
- Current implemented OpenAPI: <http://127.0.0.1:8080/openapi.json>

Protected API endpoints expect:

```http
Authorization: Bearer dev-local-token
```

The prototype intentionally keeps Feishu, Dify, GitHub Actions, and webhook
integrations as managed connector records first. Secret material is stored only
for local development execution and is not returned by read APIs. Dify chat and
Feishu message calls are available only through the Tool Gateway flow: approval,
queued `tool:*` run, worker claim, current `lease_token`, and worker-scoped
`/tools/execute`. Broader connector surfaces remain future work.

The default development token is accepted only for localhost binding. If the
service is bound to a non-localhost host, set `CLOUDAGENT_AUTH_TOKEN` or pass
`--auth-token` explicitly. The admin page no longer embeds a default token or
worker run lease tokens.

Implemented local runtime surfaces include:

- Agent, Environment, Session, ordered Event Store, and SSE replay.
- Built-in Permission Profiles and Sandbox Profiles, exposed through
  `/api/v1/permission-profiles` and `/api/v1/sandbox-profiles`. Environment
  records now persist `permission_profile_id`, `sandbox_profile_id`,
  package policy, and per-environment tool policy defaults. The profile
  vocabulary includes `read-only`, `workspace-write`, and `network-limited`;
  `danger-full-access` is intentionally visible but blocked for environment
  creation in this prototype. Only implemented sandbox profiles can create
  environments; planned/reference sandbox profiles stay visible as roadmap
  contracts but are blocked until the provider exists.
- Workers, Jobs, Runs, signed integration webhooks, scheduler delay triggers, and
  session user-event turns. Job triggers, scheduler delay triggers, signed
  integration webhooks, and `user.*` session events create queued runs and return
  before adapter execution. `/api/v1/jobs/{job_id}/enqueue` remains an explicit
  queueing alias for worker `claim` and worker-side execution.
- A dependency-free `cloudagent-worker` CLI that registers over HTTP, claims a
  queued run, starts a turn, executes the local prototype adapter in the worker
  process, writes events/artifacts/usage back through worker-scoped API
  endpoints, and exits or loops. Each claim returns a `lease_token` and
  `lease_generation`; worker-side writeback and the legacy `/execute` path must
  present the current token, so stale workers cannot complete a run after lease
  expiry and re-claim. During worker-local execution, the CLI renews the active
  run lease through `/api/v1/workers/{worker_id}/runs/{run_id}/lease/renew`.
  A legacy `--server-execute` flag still calls the server-side `/execute` path
  for compatibility. This is a local prototype worker client, not a hardened
  remote fleet yet.
- A `LocalPrototypeAdapter` runtime boundary backed by a fixed
  `LocalSubprocessSandboxProvider`. It emits `kernel.*`, `sandbox.*`,
  `runtime.policy_applied`, `worker.*`, artifact, usage, and audit-correlated
  events, creates a temporary workspace, runs a controlled Python subprocess
  without shell expansion, and deletes the workspace after the turn. Worker-side
  `/turn/start` returns the effective runtime policy so the worker and control
  plane report the same sandbox/permission contract.
- A probe-only `CodexCliProbeAdapter` kernel entry at `codex-cli-probe`. It
  locates `codex`, runs `codex --version`, and reports availability without
  executing prompts, tools, file edits, or shell commands.
- Minimal Tool Gateway policy flow with `always_allow`, `always_ask`, approval
  resolve, worker-scoped approved action execution, and audit events. Control
  plane requests record/approve/queue tool actions; workers execute approved
  actions through lease-checked worker endpoints. `/api/v1/tools` returns an
  effective policy matrix with `allow` / `ask` / `deny` decisions and policy
  source for each built-in tool. The current connector-backed built-ins cover
  `integration.dify.chat` and `integration.feishu.message`.
- Minimal Vault API for write-only credential registration. `/api/v1/vaults`
  and `/api/v1/vaults/{vault_id}/credentials` retain only redacted auth
  metadata and `secret_ref` digests; plaintext credential material is discarded
  after the request. Sessions can bind `vault_ids`, and runtime policy events
  report the bound IDs and count. Runtime secret injection remains disabled
  until a KMS or broker-backed provider is implemented.
- JSON-backed local Files, session Artifacts, and placeholder Usage records for
  completed turns.
