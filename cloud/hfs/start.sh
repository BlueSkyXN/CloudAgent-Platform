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
runtime_release="${CLOUDAGENT_RUNTIME_RELEASE:-}"
runtime_version="${CLOUDAGENT_RUNTIME_VERSION:-}"
runtime_git_sha="${CLOUDAGENT_RUNTIME_GIT_SHA:-}"
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  else
    PYTHON_BIN="python"
  fi
fi

if [[ -z "${runtime_release}" || -z "${runtime_version}" || -z "${runtime_git_sha}" ]]; then
  log "CLOUDAGENT_RUNTIME_RELEASE, CLOUDAGENT_RUNTIME_VERSION, and CLOUDAGENT_RUNTIME_GIT_SHA must pin one immutable runtime release"
  exit 65
fi
if ! [[ "${runtime_version}" =~ ^[0-9A-Za-z._-]+$ ]] || ! [[ "${runtime_git_sha}" =~ ^[0-9a-f]{40}$ ]] || ! [[ "${runtime_release}" =~ ^v[0-9A-Za-z._-]+-[0-9a-f]{40}$ ]]; then
  log "runtime release pin contains an unsafe version, release ID, or Git SHA"
  exit 65
fi
if [[ "${runtime_release}" != "v${runtime_version}-${runtime_git_sha}" ]]; then
  log "runtime release ID must bind the configured version and Git SHA"
  exit 65
fi

runtime_dir="${CLOUDAGENT_RUNTIME_ROOT}/releases/${runtime_release}"
runtime_app="${runtime_dir}/src/cloudagent_platform/app.py"
if [[ ! -f "${runtime_app}" ]]; then
  log "pinned runtime source missing at ${runtime_app}"
  log "mount the runtime release bucket at ${CLOUDAGENT_RUNTIME_ROOT} and select an existing immutable release"
  if is_truthy "${CLOUDAGENT_HFS_ALLOW_PROBE_FALLBACK:-false}"; then
    log "falling back to deployment probe because CLOUDAGENT_HFS_ALLOW_PROBE_FALLBACK=true"
    exec "${PYTHON_BIN}" /app/app.py
  fi
  exit 66
fi

verified_runtime_root="${CLOUDAGENT_VERIFIED_RUNTIME_ROOT:-${TMPDIR:-/tmp}}"
mkdir -p "${verified_runtime_root}"
verified_runtime_dir="$(mktemp -d "${verified_runtime_root%/}/cloudagent-verified-runtime.XXXXXX")"
if ! "${PYTHON_BIN}" - "${runtime_dir}" "${verified_runtime_dir}" "${runtime_release}" "${runtime_version}" "${runtime_git_sha}" <<'PY'; then
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path, PurePosixPath

runtime_dir = Path(sys.argv[1])
verified_runtime_dir = Path(sys.argv[2])
release_id, version, git_sha = sys.argv[3:]
manifest_path = runtime_dir / "RUNTIME_MANIFEST.json"
try:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"invalid runtime manifest: {exc}") from exc

if manifest.get("schema_version") != 2:
    raise SystemExit("unsupported runtime manifest schema")
if manifest.get("git_dirty"):
    raise SystemExit("dirty runtime manifest cannot be started as a release")
for key, expected in (("release_id", release_id), ("version", version), ("git_sha", git_sha)):
    if manifest.get(key) != expected:
        raise SystemExit(f"runtime manifest {key} does not match configured pin")
if release_id != f"v{version}-{git_sha}":
    raise SystemExit("configured release id does not bind the configured version and git SHA")
files = manifest.get("files")
if not isinstance(files, list) or not files:
    raise SystemExit("runtime manifest must contain a non-empty files list")
manifest_paths: set[str] = set()
for item in files:
    if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("sha256"), str):
        raise SystemExit("runtime manifest contains an invalid file entry")
    relative = PurePosixPath(item["path"])
    if not item["path"] or relative.is_absolute() or ".." in relative.parts:
        raise SystemExit("runtime manifest contains an unsafe file path")
    normalized = relative.as_posix()
    if normalized in manifest_paths:
        raise SystemExit("runtime manifest contains duplicate file paths")
    manifest_paths.add(normalized)


def verify_tree(root: Path) -> None:
    for item in files:
        relative = PurePosixPath(item["path"])
        path = root.joinpath(*relative.parts)
        try:
            if path.is_symlink():
                raise SystemExit(f"runtime manifest file must not be a symlink: {relative.as_posix()}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise SystemExit(f"runtime manifest file is missing: {item['path']}") from exc
        if digest != item["sha256"]:
            raise SystemExit(f"runtime manifest hash mismatch: {item['path']}")
        if item.get("bytes") != path.stat().st_size:
            raise SystemExit(f"runtime manifest size mismatch: {item['path']}")

    actual_paths: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SystemExit(f"runtime release must not contain symlinks: {path.relative_to(root)}")
        if path.is_file() and path.name != "RUNTIME_MANIFEST.json":
            actual_paths.add(path.relative_to(root).as_posix())
    if actual_paths != manifest_paths:
        missing = sorted(manifest_paths - actual_paths)
        unexpected = sorted(actual_paths - manifest_paths)
        raise SystemExit(f"runtime manifest inventory mismatch: missing={missing!r} unexpected={unexpected!r}")


verify_tree(runtime_dir)
shutil.copytree(runtime_dir, verified_runtime_dir, dirs_exist_ok=True, symlinks=False)
verify_tree(verified_runtime_dir)
PY
  log "pinned runtime manifest validation failed"
  rm -rf "${verified_runtime_dir}"
  exit 65
fi
runtime_dir="${verified_runtime_dir}"
runtime_app="${runtime_dir}/src/cloudagent_platform/app.py"

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
export PYTHONPATH="${runtime_dir}/src${PYTHONPATH:+:${PYTHONPATH}}"

log "starting pinned CloudAgent runtime ${runtime_release} from ${runtime_dir}"
log "database=${CLOUDAGENT_DB}"
exec "${PYTHON_BIN}" -m cloudagent_platform.app \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --database "${CLOUDAGENT_DB}"
