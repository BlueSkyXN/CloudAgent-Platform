# CloudAgent-Platform

CloudAgent-Platform is an API-first control plane for governed Agent execution.
It combines scheduling, session/event handling, worker leases, runtime policy,
tool approvals, artifacts, usage evidence, Vault references, and a responsive
operator console in one dependency-free Python service.

`v0.3.0` is the current **company-showcase release** for the local runtime. It
demonstrates a complete, honest execution loop; it does not claim production
multi-tenant isolation, a hardened remote worker fleet, or runtime secret
injection.

## What the Showcase Proves

The console and API operate on the same persisted SQLite state. There are no
mock cards or decorative actions in the main workflow:

1. Create or bootstrap governed Agent, Environment, Session, Job, and Worker
   resources.
2. Queue work through the Job `Queue run` action or a `user.message` Session
   event.
3. Claim the run with a lease-scoped Worker.
4. Execute the local adapter or an exact approval-bound tool action under the
   selected permission, sandbox, and Tool Policy.
5. Inspect ordered events, downloadable artifacts, usage, approval reasons,
   and correlated audit evidence.

The console's signature `Runtime Rail` exposes that path as live state:

```text
API Gateway -> Policy -> Queue -> Worker -> Artifact / Audit evidence
```

## Run Locally

Start the standard-library service:

```bash
PYTHONPATH=src \
  CLOUDAGENT_AUTH_TOKEN=dev-local-token \
  python3 -m cloudagent_platform --port 8080
```

Open the operator console at <http://127.0.0.1:8080/admin> and enter the same
Bearer token. The token is retained only in browser `sessionStorage`; it is not
embedded in the page, URL, API output, or persisted application data.

Useful endpoints:

- Console: <http://127.0.0.1:8080/admin>
- Health: <http://127.0.0.1:8080/_ops/healthz>
- Readiness: <http://127.0.0.1:8080/_ops/readyz>
- Implemented OpenAPI: <http://127.0.0.1:8080/openapi.json>
- Runtime/SDLC boundary: <http://127.0.0.1:8080/api/v1/sdlc/status>

Protected endpoints expect:

```http
Authorization: Bearer dev-local-token
```

The default development token is accepted only for localhost binding. A
non-loopback bind requires `CLOUDAGENT_AUTH_TOKEN` or `--auth-token`.

### Fast Showcase Flow

1. Connect with the local token.
2. Select **Initialize showcase** on an empty workspace. The bootstrap API is
   idempotent and reuses the marked resources on subsequent calls.
3. Open **Runtime** and process the bootstrap run with the registered showcase
   Worker. Use **Queue run** when you want another Job execution.
4. Open **Governance** to review effective permission/sandbox/tool policy. To
   demonstrate an approval, create an `artifact.create` policy with
   `always_ask`.
5. Open **Sessions**, submit an `artifact.create` request with JSON arguments,
   review the proposed arguments, approve it with a reason, then return to
   **Runtime** and process the exact queued tool run.
6. Reopen the Session to inspect the timeline, usage and audit correlation, or
   download its artifact from the drawer or **Resources**.

## Architecture

```text
Web Console / API clients
          |
          v
HTTP + auth + OpenAPI + SSE
          |
          +---- ShowcaseService read model / idempotent bootstrap
          |
          v
SQLite control plane + ordered Event Store + Audit / Usage
          |
          v
Run queue + lease-scoped Worker API
          |
          v
Runtime Adapter -> Sandbox Provider -> Tool Gateway -> Artifact evidence
```

Important boundaries:

- The Web Console consumes protected HTTP APIs; it does not read SQLite or
  bypass worker lease and policy checks.
- `ShowcaseService` composes operator-facing readiness/activity data without
  moving execution into the control plane.
- Session events are durable runtime truth; SSE is a replay/transport surface.
- A Session may queue multiple runs, but only one run per Session can be
  claimed at a time. This serialization is enforced inside the local Store
  process and is not presented as distributed coordination.
- Kernel-specific behavior stays behind Runtime Adapter contracts.
- Environment policy is fail-closed: request payloads cannot weaken the chosen
  permission or sandbox profile.
- Approved tool actions are bound to one exact run and lease generation before
  execution. Concurrent replay cannot enter the connector twice in the local
  process.
- Vault credentials are write-only references. The prototype stores redacted
  metadata and `secret_ref` digests, then discards submitted plaintext.
- Integration credentials are also never persisted as plaintext. They live
  only in current-process memory, so a restarted service reports
  `credential_required` and requires explicit re-registration.
- Dify/Feishu calls are approval- and lease-gated control-plane connectors.
  Provider-specific idempotency is not implemented: an ambiguous remote
  failure is recorded as `failed` and is never replayed automatically.
- Connector targets are resolved once per request, every resolved address must
  be public, and the HTTP/TLS connection is pinned to a validated address.
  Redirects are not followed, upstream errors are redacted, and response bodies
  are capped at 1 MiB.

## Implemented Surfaces

### Control plane and runtime

- Agent, Environment, Session, ordered Event Store, SSE replay, Jobs, Runs, and
  Workers.
- Worker claim, lease generation/token validation, lease renewal, worker-side
  turn start, event/artifact/usage writeback, tool execution, and completion.
- `LocalPrototypeAdapter` with a fixed `LocalSubprocessSandboxProvider` that
  uses a temporary workspace, avoids shell expansion, emits policy/sandbox/
  kernel events, records evidence, and removes the workspace after the turn.
- Probe-only `CodexCliProbeAdapter` that reports `codex --version` availability
  without executing prompts, tools, edits, or shell tasks.

### Governance and resources

- Built-in `read-only`, `workspace-write`, and `network-limited` Permission
  Profiles. `danger-full-access` is visible for vocabulary parity but blocked.
- Implemented local subprocess sandbox plus planned/reference profiles that are
  clearly labeled and cannot create Environments.
- Tool Gateway decisions (`allow`, `ask`, `deny`), pending approvals, and
  worker-scoped approved execution bound to a specific queued run.
- Reference-only tool vocabulary is fail-closed: it is labeled as unavailable,
  cannot be enabled by policy, and cannot create an action or queued run.
- JSON/text Files, authenticated Artifact content downloads, Usage records,
  managed Integrations, process-memory Integration credential registration,
  and write-only Vault credential registration.

### Operator experience

- Authenticated access gate with no default token embedded in the page.
- Overview with live readiness, runtime rail, signals, activity, and capability
  boundaries.
- Runtime, Sessions, Governance, and Resources workspaces with functional
  create, queue, claim/execute, tool request/approval, file and artifact
  download, and evidence inspection flows.
- URL-addressable workspace and Session selection with browser Back/Forward
  restoration; bearer credentials remain in `sessionStorage` and never enter
  URLs. Session message drafts are scoped to the selected Session and survive
  recoverable navigation within the browser session.
- Calm Precision semantic tokens, near-black primary actions, restrained green
  selection emphasis, flat data hierarchy, visible high-contrast keyboard
  focus, 44px phone targets, and 16px phone form controls.
- `GET /api/v1/admin/sessions/{session_id}/workspace` aggregates the protected
  Session trace, approvals, evidence, usage, audit, and tools for the Console
  without weakening any worker, lease, policy, or credential boundary.
- Loading, empty, disabled, success, and persistent form-error states;
  responsive layout; modal focus trapping and restoration; reduced-motion
  support; strict static-asset CSP.

## Capability Boundary

| Capability | Current state |
|---|---|
| Local control plane and SQLite event/audit evidence | Implemented |
| Local worker and lease-scoped execution loop | Implemented |
| Company-showcase Web Console | Implemented |
| Permission and sandbox policy contracts | Implemented for local provider |
| Connector records and gated Dify/Feishu tool calls | Implemented local flow |
| Integration credential persistence | Process memory only; re-register after restart |
| Vault plaintext persistence | Prohibited |
| Vault runtime secret injection | Not implemented |
| Production-grade sandbox isolation | Not implemented |
| Hardened distributed worker fleet / multi-tenant HA | Not implemented |

## External Worker

Run one worker pass against the service:

```bash
PYTHONPATH=src python3 -m cloudagent_platform.worker \
  --base-url http://127.0.0.1:8080 \
  --token dev-local-token \
  --worker-id worker_local_http \
  --once
```

The browser showcase uses the compatibility `/execute` path for adapter runs
and the worker-scoped `/tools/execute` path for approval-bound tool runs. It
keeps the returned `lease_token` only in the current JavaScript call. The
standalone worker remains the preferred demonstration of the
control-plane/execution-plane boundary.

## Validation

Run the release hardening gate:

```bash
python3 -m py_compile src/cloudagent_platform/*.py tests/*.py cloud/hfs/validate_source_wrapper.py
PYTHONPATH=src python3 -W error::ResourceWarning -m unittest discover -s tests
node --check src/cloudagent_platform/web/console.js
python3 -m pip wheel . --no-deps --wheel-dir /tmp/cloudagent-wheel
bash -n cloud/hfs/build_runtime_snapshot.sh cloud/hfs/export_space_bundle.sh cloud/hfs/healthcheck.sh cloud/hfs/smoke_source_wrapper.sh cloud/hfs/smoke_mounted_runtime.sh cloud/hfs/start.sh
bash cloud/hfs/export_space_bundle.sh /tmp/cloudagent-platform-hfs-space
python3 cloud/hfs/validate_source_wrapper.py
git diff --check
```

CI repeats the Python matrix, JavaScript syntax, wheel, and HFS source-wrapper
export/provenance contract checks on pushes to `main` and pull requests. It does
not run a Docker build because that build fetches the public source commit.

## Deployment

`cloud/hfs/` is the Hugging Face Docker Space source wrapper. It exports a flat,
safe Space root without product source, then the Docker build fetches
`https://github.com/BlueSkyXN/CloudAgent-Platform.git` at the emitted full
commit SHA and verifies detached `HEAD` before launching the real package.
`CLOUDAGENT_AUTH_TOKEN` is a Space Secret; the SQLite control plane requires an
existing writable persistent `/data` mount, initializes its application directory
below that mount, and never falls back to `/tmp`.

The local source-wrapper contract validates only static/exported evidence.
Docker build, Space deployment and readback, live health/auth smoke, restart
persistence, backup, and isolated restore verification require a separately
approved environment. Artifact-lane reclassification remains owner-gated by an
observed source-build threshold; this repository supplies no such evidence.

`local/20260616/` contains the SDLC, threat model, data model, validation plan,
roadmap, and deployment records. The implemented API contract is always the
document served by `/openapi.json` from the current code.
