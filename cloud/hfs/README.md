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

It runs a pinned CloudAgent runtime release from a Hugging Face bucket mounted
into the Docker Space. The Space repository remains a flat wrapper; product
source is mounted at runtime instead of being copied into the Space root.

Runtime contract:

- Docker Space starts on port `7860`.
- Source bucket is mounted read-only at `/mnt/cloudagent-runtime`, with each
  release under `releases/v<version>-<git-sha>/`.
- `CLOUDAGENT_RUNTIME_RELEASE`, `CLOUDAGENT_RUNTIME_VERSION`, and
  `CLOUDAGENT_RUNTIME_GIT_SHA` must select the same immutable release.
- `CLOUDAGENT_AUTH_TOKEN` must be configured as a Hugging Face Space Secret.
- `/_ops/healthz`, `/_ops/readyz`, and `/openapi.json` are public operational
  surfaces.
- `/api/v1/*` application routes keep the runtime bearer-token boundary.
- Runtime releases at `0.2.0` or later expose the package-owned operator
  Console at `/admin` with same-origin CSS/JS/SVG assets. The Console does not
  embed the Space secret and still requires the runtime Bearer token.

Startup verifies the selected `RUNTIME_MANIFEST.json` version, Git SHA, and all
file hashes before serving traffic, copies the verified release into the
container's ephemeral `/tmp`, then verifies that copied inventory again.
Deployment truth is read from both the Space wrapper SHA and the selected bucket
manifest; a `RUNNING` Space alone does not prove that the mounted runtime
matches the intended release.

## HFS Contract

- SDK: Docker
- Public port: `7860`
- Canonical health endpoint: `/_ops/healthz`
- Readiness endpoint: `/_ops/readyz`
- Runtime mode: `bucket-mounted-runtime`
- Runtime source mount: `hf://buckets/BlueSkyXN/cloudagent-platform-hfs-runtime:/mnt/cloudagent-runtime:ro`
- Runtime release pin: `releases/v<version>-<git-sha>/` plus matching Space
  variables `CLOUDAGENT_RUNTIME_RELEASE`, `CLOUDAGENT_RUNTIME_VERSION`, and
  `CLOUDAGENT_RUNTIME_GIT_SHA`
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

Build the bucket artifact only from a clean commit:

```bash
bash cloud/hfs/build_runtime_snapshot.sh /tmp/cloudagent-runtime
```

Sync the resulting `releases/<release-id>/` directory without replacing prior
releases. Roll back by pointing the three Space pin variables at a previously
verified release directory, restarting the Space, and re-reading its manifest
and health surfaces.

## Source

Primary planning source: `local/20260616` in
<https://github.com/BlueSkyXN/CloudAgent-Platform>.
