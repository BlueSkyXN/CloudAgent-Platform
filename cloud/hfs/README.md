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

This flat Hugging Face Docker Space wrapper builds the public CloudAgent source
at an immutable Git commit. The Space repository contains deployment files only;
product source is fetched during the Docker build and is never exported into the
Space root.

## Source contract

- Lane: `source`; version source: full 40-character Git commit SHA.
- The exported `Dockerfile`, `BUILD_SOURCE.txt`, and `BUNDLE_MANIFEST.json` bind
  the same `source_commit` from
  `https://github.com/BlueSkyXN/CloudAgent-Platform.git`.
- Docker fetches that commit, checks out detached `HEAD`, and rejects any mismatch
  before it starts the product package.
- The public port is `7860`. `/_ops/healthz`, `/_ops/readyz`, and `/openapi.json`
  remain public operational surfaces; `/api/v1/*` retains its Bearer-token
  boundary, including `/api/v1/system/info` and the operator Console APIs.
- `CLOUDAGENT_AUTH_TOKEN` is the sole required Space Secret. Register key names
  only; never put values in this wrapper or its source repository.

## Persistence and readiness

The control plane continues to use SQLite. Startup requires the Space persistent
storage mount to provide an existing writable `/data` directory, initializes its
application directory below that mount, and uses
`/data/cloudagent/cloudagent-platform.sqlite3` by default. A missing, symlinked,
or non-writable mount, an invalid source checkout, a missing secret, or a failed
SQLite read/write probe stops startup; there is no ephemeral or probe-server
fallback.

The package itself owns schema initialization and existing persistence behavior.
Production backup, restart persistence, and isolated restore evidence remain
owner-gated runtime verification, not assertions supplied by this repository.

## Export and checks

Create a flat wrapper only from a clean commit:

```bash
bash cloud/hfs/export_space_bundle.sh /tmp/cloudagent-platform-hfs-space
python3 cloud/hfs/validate_source_wrapper.py
```

The validator checks the source-pin handoff, flat export boundary, generated
provenance, health-check wiring, and bootstrap fail-closed guards. The exporter
stages its output in a fresh sibling directory and renames it only after the
inventory and denylist checks pass, so a failed export leaves no publishable
partial bundle. It does not build Docker or contact GitHub/Hugging Face. There
is no dirty-tree bypass: local uncommitted review uses static tests, not an
export whose bytes could be mistaken for the current commit.

Docker build, Space deployment, remote readback, live health/auth smoke, SQLite
restart persistence, and backup/restore checks require an approved environment
and are intentionally not performed by this local wrapper contract.

## Owner decisions retained

- Define the observed source-build duration/resource threshold that would allow a
  future artifact-lane reclassification.
- Keep the historical dated Space as a rollback target during the observation
  window; the approved production target is `BlueSkyXN/cloudagent-platform-hfs`.
- Decide the retention and decommission plan for the previous runtime bucket.
