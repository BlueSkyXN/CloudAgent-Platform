#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '[cloudagent-hfs] %s\n' "$*" >&2
}

is_truthy() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    true|1|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

export PORT="${PORT:-7860}"
export CLOUDAGENT_HFS_MODE="${CLOUDAGENT_HFS_MODE:-bucket-mounted-runtime}"
export CLOUDAGENT_RUNTIME_ROOT="${CLOUDAGENT_RUNTIME_ROOT:-/mnt/cloudagent-runtime}"
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  else
    PYTHON_BIN="python"
  fi
fi

runtime_app="${CLOUDAGENT_RUNTIME_ROOT}/src/cloudagent_platform/app.py"
if [[ ! -f "${runtime_app}" ]]; then
  log "runtime source missing at ${runtime_app}"
  log "mount the runtime source bucket at ${CLOUDAGENT_RUNTIME_ROOT}"
  if is_truthy "${CLOUDAGENT_HFS_ALLOW_PROBE_FALLBACK:-false}"; then
    log "falling back to deployment probe because CLOUDAGENT_HFS_ALLOW_PROBE_FALLBACK=true"
    exec "${PYTHON_BIN}" /app/app.py
  fi
  exit 66
fi

if [[ -z "${CLOUDAGENT_AUTH_TOKEN:-}" ]]; then
  log "CLOUDAGENT_AUTH_TOKEN must be configured as a Hugging Face Space Secret"
  exit 64
fi

if [[ -z "${CLOUDAGENT_DB:-}" ]]; then
  if mkdir -p /data/cloudagent 2>/dev/null && [[ -w /data/cloudagent ]]; then
    export CLOUDAGENT_DB="/data/cloudagent/cloudagent-platform.sqlite3"
  else
    export CLOUDAGENT_DB="/tmp/cloudagent-platform.sqlite3"
  fi
fi

mkdir -p "$(dirname "${CLOUDAGENT_DB}")"
export PYTHONPATH="${CLOUDAGENT_RUNTIME_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

log "starting CloudAgent runtime from ${CLOUDAGENT_RUNTIME_ROOT}"
log "database=${CLOUDAGENT_DB}"
exec "${PYTHON_BIN}" -m cloudagent_platform.app \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --database "${CLOUDAGENT_DB}"
