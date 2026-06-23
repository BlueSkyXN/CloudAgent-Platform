# cloud/hfs navigation card

Hugging Face Docker Space wrapper for CloudAgent-Platform. This directory is
deployment packaging, not the product source tree.

## Root layering

- Hugging Face Space root is the flat export produced by
  `bash cloud/hfs/export_space_bundle.sh /tmp/cloudagent-platform-hfs-space`.
- Exported Space roots contain only this wrapper's files directly.
- Do not copy `local/`, `.git/`, `.env`, logs, generated artifacts, runtime
  data, or future product source trees into the Space root unless a later HFS
  contract explicitly allows it.
- Keep `app_port`, `EXPOSE`, runtime `PORT`, and health endpoints aligned at
  `7860`.

## Current mode

This wrapper is a P0 HFS deployment probe. It proves the repo can be deployed
to a real Hugging Face Docker Space and exposes SDLC/runtime contract metadata.
It is not the full CloudAgent runtime implementation.

## Required before changes

- Read `README.md`, `hfs-dev.toml`, `Dockerfile`, `app.py`, and
  `export_space_bundle.sh`.
- If adding product runtime code, update `hfs-dev.toml` and
  `local/20260616/60-hfs-deployment.md`.
- If adding secrets, use Hugging Face Secrets only; never commit real tokens or
  generated secret files.

## Validation

- `bash cloud/hfs/export_space_bundle.sh /tmp/cloudagent-platform-hfs-space`
- `bash -n cloud/hfs/export_space_bundle.sh cloud/hfs/healthcheck.sh`
- HFS live smoke: `/_ops/healthz`, `/_ops/readyz`, `/api/v1/system/info`,
  `/api/v1/sdlc/status`, `/openapi.json`
