# Changelog

All notable changes to CloudAgent-Platform are documented here.

## Unreleased

### Changed

- Made approval-to-run binding transactional and recoverable, and made expired
  tool leases converge fail-closed instead of replaying an ambiguous external
  side effect.
- Made Connector failures terminate both the action and its bound run, blocked
  generic worker execution/completion from bypassing the tool state machine,
  and added DNS-pinned public-address validation, redirect refusal, redacted
  upstream errors, and a 1 MiB response limit.
- Added OpenAPI route-contract validation for path parameters, operation IDs,
  local references, and success/error responses.
- Made the HFS runtime release pin immutable: Space startup now validates the
  selected release ID, version, Git SHA, and every manifest file hash before
  launching the application.
- Added a clean-tree runtime snapshot builder and made dirty Space exports fail
  by default. Explicit dirty overrides are marked non-release provenance and
  cannot be started as a pinned release.
- Corrected the Console Integration workflow so inbound Webhook credentials can
  be registered without an outbound base URL; outbound providers still require
  their base URL.
- Rebuilt the Console around the Calm Precision semantic palette, typography,
  spacing, flat information hierarchy, accessible focus treatment, 44px mobile
  targets, 16px mobile form controls, and interruptible reduced-motion states.
- Added shareable URL state for every Console workspace and selected Session,
  including browser Back/Forward restoration and session-scoped draft recovery.
- Added a typed admin Session workspace read model so the Console can load the
  Session, timeline, approvals, evidence, usage, audit, and available tools with
  one authenticated request instead of six independent collection reads.

## 0.2.0 - 2026-07-15

### Added

- Company-showcase Web Console with authenticated Access Gate, live Runtime
  Rail, Runtime, Sessions, Governance, and Resources workspaces.
- Idempotent `/api/v1/admin/showcase/bootstrap` topology for a real Agent,
  Environment, Session, Job, Worker, and queued Run.
- `ShowcaseService` read model for readiness, queue/worker signals, redacted
  activity, and explicit production capability boundaries.
- Global Artifact and Tool Policy list endpoints.
- Protected Artifact metadata/content endpoints and authenticated Console
  downloads from both Session evidence and Resources.
- Write-only `POST /api/v1/integrations/{integration_id}/credential`
  registration with process-memory lifecycle and restart-visible status.
- Session drawer tool-request, approval-reason, usage-summary, and correlated
  audit workflows.
- Responsive/mobile navigation, keyboard focus, reduced-motion behavior,
  loading/empty/error states, modal/drawer workflows, and expandable event
  payloads.
- Strict same-origin static asset CSP, clickjacking protection, Permissions
  Policy, packaged console assets, favicon, and Python 3.11-3.13 CI.

### Changed

- Replaced the inline prototype admin page with package-owned HTML/CSS/JS
  assets served from an allowlist.
- Bumped the package version from `0.1.0` to `0.2.0`.
- Updated runtime status and documentation to distinguish a local showcase
  candidate from production sandbox, Vault injection, and distributed fleet
  readiness.
- Made reference-only tool vocabulary fail-closed so unavailable HTTP/shell
  adapters cannot be enabled, requested, approved, or queued as if complete.
- Serialized run claims per Session and derived Session state from durable
  queued/running work instead of allowing concurrent turns to overwrite it.
- Bound approved tool actions to an exact run and lease generation before
  execution; concurrent replay now loses the execution CAS before side effects.
- Removed Integration plaintext secret storage. New credentials are held only
  in process memory; legacy SQLite columns are removed and vacuumed at startup.
- Corrected OpenAPI security for public operations and signed webhooks, and
  applied `Cache-Control: no-store` to dynamic API/download responses.
- Replaced duplicate Job trigger/queue controls with one truthful **Queue run**
  action and completed modal/drawer focus trapping, inert background behavior,
  async focus recovery, and stale-view response fencing.
- Hardened the mounted-runtime release smoke with a configurable 60-second
  startup window, early process-exit and socket diagnostics, and bounded
  cleanup so CI failures remain actionable instead of hanging or timing out
  without evidence.

### Removed

- Removed the legacy inline `admin.py` console implementation.

### Validation

- 32 Python unit/integration tests, including concurrency, lease fencing,
  legacy secret migration, OpenAPI auth modes, and Console asset contracts.
- JavaScript syntax, OpenAPI reference, wheel/package-data and installed-wheel
  runtime checks, HFS wrapper export, and mounted-runtime startup checks.
- Browser validation covers auth failure/success, bootstrap, run
  claim/execute, governed tool approval, artifact download, focus management,
  desktop, tablet, and mobile layouts.

### Deployment boundary

- The company-showcase release is accepted only when GitHub `main`, the HFS
  runtime bucket manifest, and the live Space OpenAPI report the released
  source/version through independent readback.
- Production sandbox isolation, runtime Vault injection, provider-specific
  connector idempotency, and a distributed multi-tenant fleet remain explicit
  non-goals for this release.
