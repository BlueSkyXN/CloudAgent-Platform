---
title: CloudAgent Platform
emoji: 🚀
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
suggested_hardware: cpu-basic
pinned: false
---

# CloudAgent-Platform HFS

This Space is the CloudAgent-Platform Hugging Face Docker Space deployment
probe.

It currently verifies the HFS packaging and runtime surface for the 20260616
SDLC package:

- Docker Space starts on port `7860`.
- `/_ops/healthz` reports runtime health.
- `/_ops/readyz` reports readiness and dependency status.
- `/api/v1/system/info` exposes HFS deployment metadata.
- `/api/v1/sdlc/status` exposes the current SDLC package status.
- `/openapi.json` exposes a minimal OpenAPI contract for the deployment probe.

This is not yet the full CloudAgent runtime. The production product remains a
multi-kernel, API-first Agent Runtime Platform with Kernel Adapter, Sandbox
Matrix, Event Store, Tool Gateway, Vault, Artifact, Audit, and Usage surfaces as
defined in the SDLC package.

## HFS Contract

- SDK: Docker
- Public port: `7860`
- Canonical health endpoint: `/_ops/healthz`
- Readiness endpoint: `/_ops/readyz`
- Runtime mode: `deployment-probe`; `full-runtime` must fail closed unless its
  dependencies are explicitly configured.
- Export provenance: `BUILD_SOURCE.txt` and `BUNDLE_MANIFEST.json`
- Export command:

```bash
bash cloud/hfs/export_space_bundle.sh /tmp/cloudagent-platform-hfs-space
```

## Source

Primary planning source: `local/20260616` in
<https://github.com/BlueSkyXN/CloudAgent-Platform>.
