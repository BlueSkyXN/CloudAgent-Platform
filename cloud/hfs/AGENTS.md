# cloud/hfs navigation card

Hugging Face Docker Space wrapper for CloudAgent-Platform. This directory is
deployment packaging, not the product source tree.

## Root layering

- Hugging Face Space root is the flat export produced by
  `bash cloud/hfs/export_space_bundle.sh /tmp/cloudagent-platform-hfs-space`.
- Exported Space roots contain only wrapper files directly. The Docker build
  fetches the public product source at the emitted immutable commit SHA.
- Do not copy `local/`, `.git/`, `.env*`, logs, generated artifacts, runtime
  data, credentials, or product source trees into the Space root.
- Keep `app_port`, `EXPOSE`, runtime `PORT`, and health endpoints aligned at
  `7860`.

## Source-wrapper contract

- `hfs-dev.toml` is the minimal HFS v2 semantic registry. Runtime pins and
  bootstrap assertions belong in `Dockerfile`, `start.sh`, and generated
  provenance files.
- `Dockerfile` must fetch, detached-checkout, and compare a full Git SHA before
  running the package.
- `start.sh` must fail closed for a missing `CLOUDAGENT_AUTH_TOKEN`, invalid
  source provenance, missing/unwritable persistent `/data`, or SQLite failure.
  It may initialize the application directory below that mounted path.
  Do not introduce runtime-mount, `/tmp`, or probe-server fallback paths.
- `healthcheck.sh` checks the running product's `/_ops/healthz`; it must never
  start a second server.

## Required before changes

- Read `README.md`, `hfs-dev.toml`, `Dockerfile`, `start.sh`,
  `healthcheck.sh`, and `export_space_bundle.sh`.
- If adding a Space setting, register its key name in `hfs-dev.toml` and use a
  harmless empty/default value only in `.env.example`.
- If adding secrets, use Hugging Face Secrets only; never commit real tokens or
  generated secret files.

## Validation

- `bash cloud/hfs/export_space_bundle.sh /tmp/cloudagent-platform-hfs-space`
- `python3 cloud/hfs/validate_source_wrapper.py`
- `bash -n cloud/hfs/export_space_bundle.sh cloud/hfs/healthcheck.sh cloud/hfs/start.sh`
- HFS live smoke, when separately approved: `/_ops/healthz`, `/_ops/readyz`,
  authenticated `/api/v1/system/info`, `/api/v1/sdlc/status`, and `/openapi.json`.
