#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '[cloudagent-hfs] %s\n' "$*" >&2
}

fail() {
  log "$*"
  exit 65
}

export PORT="${PORT:-7860}"
if [[ "${PORT}" != "7860" ]]; then
  fail "PORT must remain 7860 for this Docker Space"
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  fail "python3 is required"
fi

source_root="${CLOUDAGENT_SOURCE_ROOT:-/opt/cloudagent}"
source_ref="${CLOUDAGENT_SOURCE_REF:-}"
if ! [[ "${source_ref}" =~ ^[0-9a-f]{40}$ ]]; then
  fail "CLOUDAGENT_SOURCE_REF must be a full lowercase Git commit SHA"
fi
if [[ ! -d "${source_root}/.git" || ! -f "${source_root}/pyproject.toml" || ! -f "${source_root}/src/cloudagent_platform/app.py" ]]; then
  fail "checked-out CloudAgent source is incomplete at ${source_root}"
fi
if ! command -v git >/dev/null 2>&1; then
  fail "git is required to verify checked-out source provenance"
fi
if [[ "$(git -C "${source_root}" rev-parse HEAD 2>/dev/null || true)" != "${source_ref}" ]]; then
  fail "checked-out source Git HEAD does not match CLOUDAGENT_SOURCE_REF"
fi

if [[ -z "${CLOUDAGENT_AUTH_TOKEN:-}" ]]; then
  fail "CLOUDAGENT_AUTH_TOKEN must be configured as a Hugging Face Space Secret"
fi

requested_db="${CLOUDAGENT_DB:-/data/cloudagent/cloudagent-platform.sqlite3}"
if [[ ! -d /data || -L /data || ! -w /data ]]; then
  fail "persistent /data mount is missing, unsafe, or not writable"
fi

if ! canonical_db="$("${PYTHON_BIN}" /app/validate_persistent_path.py "${requested_db}")"; then
  fail "CLOUDAGENT_DB must resolve to a non-symlink file under the persistent /data/cloudagent mount"
fi
export CLOUDAGENT_DB="${canonical_db}"

db_parent="$(dirname "${CLOUDAGENT_DB}")"
if ! mkdir -p "${db_parent}" || [[ -L "${db_parent}" || ! -w "${db_parent}" ]]; then
  fail "persistent SQLite directory cannot be initialized or is not writable: ${db_parent}"
fi
if ! canonical_db="$("${PYTHON_BIN}" /app/validate_persistent_path.py "${CLOUDAGENT_DB}")"; then
  fail "persistent SQLite path contains a symlink component after initialization"
fi
export CLOUDAGENT_DB="${canonical_db}"
if [[ -L "${CLOUDAGENT_DB}" ]]; then
  fail "persistent SQLite database path must not be a symlink: ${CLOUDAGENT_DB}"
fi

if ! "${PYTHON_BIN}" - "${CLOUDAGENT_DB}" <<'PY'
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    connection = sqlite3.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE cloudagent_hfs_startup_probe (id INTEGER NOT NULL)")
        connection.execute("INSERT INTO cloudagent_hfs_startup_probe VALUES (1)")
        assert connection.execute("SELECT id FROM cloudagent_hfs_startup_probe").fetchone() == (1,)
        connection.rollback()
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
    finally:
        connection.close()
except (OSError, sqlite3.Error, AssertionError) as exc:
    raise SystemExit(f"SQLite persistence readiness check failed: {exc}") from exc
PY
then
  fail "persistent SQLite readiness check failed"
fi

export PYTHONPATH="${source_root}/src${PYTHONPATH:+:${PYTHONPATH}}"
log "starting checked-out CloudAgent source ${source_ref}"
log "database=${CLOUDAGENT_DB}"
exec "${PYTHON_BIN}" -m cloudagent_platform.app \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --database "${CLOUDAGENT_DB}"
