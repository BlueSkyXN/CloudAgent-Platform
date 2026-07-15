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

This Space is the CloudAgent-Platform Hugging Face Docker Space deployment.

It runs the current local runtime prototype from a Hugging Face bucket mounted
into the Docker Space. The Space repository remains a flat wrapper; product
source is mounted at runtime instead of being copied into the Space root.

Runtime contract:

- Docker Space starts on port `7860`.
- Source bucket is mounted read-only at `/mnt/cloudagent-runtime`.
- `CLOUDAGENT_AUTH_TOKEN` must be configured as a Hugging Face Space Secret.
- `/_ops/healthz`, `/_ops/readyz`, and `/openapi.json` are public operational
  surfaces.
- `/api/v1/*` application routes keep the runtime bearer-token boundary.
- Runtime releases at `0.2.0` or later expose the package-owned operator
  Console at `/admin` with same-origin CSS/JS/SVG assets. The Console does not
  embed the Space secret and still requires the runtime Bearer token.

Deployment truth is read from both the Space wrapper SHA and the mounted
bucket's `RUNTIME_MANIFEST.json.git_sha`; a `RUNNING` Space alone does not prove
that the mounted runtime matches GitHub `main`.

## HFS Contract

- SDK: Docker
- Public port: `7860`
- Canonical health endpoint: `/_ops/healthz`
- Readiness endpoint: `/_ops/readyz`
- Runtime mode: `bucket-mounted-runtime`
- Runtime source mount: `hf://buckets/BlueSkyXN/cloudagent-platform-hfs-runtime:/mnt/cloudagent-runtime:ro`
- Export provenance: `BUILD_SOURCE.txt` and `BUNDLE_MANIFEST.json`
- Export command:

```bash
bash cloud/hfs/export_space_bundle.sh /tmp/cloudagent-platform-hfs-space
```

Local release candidates also simulate the read-only runtime mount and start
the real package through the wrapper before any publish decision:

```bash
bash cloud/hfs/smoke_mounted_runtime.sh
```

## Source

Primary planning source: `local/20260616` in
<https://github.com/BlueSkyXN/CloudAgent-Platform>.
