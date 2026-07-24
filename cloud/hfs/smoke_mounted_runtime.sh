#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/cloudagent-hfs-smoke.XXXXXX")"
server_pid=""
snapshot_worktree=""
startup_timeout_seconds="${CLOUDAGENT_SMOKE_STARTUP_TIMEOUT_SECONDS:-60}"

cleanup() {
  if [[ -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
    forced_termination=false
    {
      kill "${server_pid}" || true
      for _ in {1..50}; do
        if ! kill -0 "${server_pid}"; then
          break
        fi
        sleep 0.1
      done
      if kill -0 "${server_pid}"; then
        forced_termination=true
        kill -9 "${server_pid}" || true
      fi
      wait "${server_pid}" || true
    } 2>/dev/null
    if [[ "${forced_termination}" == true ]]; then
      printf 'Mounted runtime did not stop after 5s; forcing termination.\n' >&2
    fi
  fi
  if [[ -n "${snapshot_worktree}" ]]; then
    git -C "${repo_root}" worktree remove --force "${snapshot_worktree}" >/dev/null 2>&1 || true
  fi
  rm -rf "${tmp_dir}"
}
trap cleanup EXIT

port="$(python3 - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"

export PORT="${port}"
export CLOUDAGENT_AUTH_TOKEN="mounted-runtime-smoke-token"
export CLOUDAGENT_DB="${tmp_dir}/cloudagent-platform.sqlite3"
export CLOUDAGENT_VERIFIED_RUNTIME_ROOT="${tmp_dir}/verified-runtime"
runtime_version="$(PYTHONPATH="${repo_root}/src" python3 -c 'from cloudagent_platform import __version__; print(__version__)')"
runtime_git_sha="$(git -C "${repo_root}" rev-parse --verify HEAD)"
runtime_release="v${runtime_version}-${runtime_git_sha}"
export CLOUDAGENT_RUNTIME_ROOT="${tmp_dir}/runtime"
export CLOUDAGENT_RUNTIME_RELEASE="${runtime_release}"
export CLOUDAGENT_RUNTIME_VERSION="${runtime_version}"
export CLOUDAGENT_RUNTIME_GIT_SHA="${runtime_git_sha}"

runtime_dir="${CLOUDAGENT_RUNTIME_ROOT}/releases/${CLOUDAGENT_RUNTIME_RELEASE}"
snapshot_worktree="${tmp_dir}/snapshot-source"
git -C "${repo_root}" worktree add --detach "${snapshot_worktree}" HEAD >/dev/null

build_runtime_snapshot() {
  rm -rf "${runtime_dir}"
  CLOUDAGENT_REPO_ROOT="${snapshot_worktree}" \
    bash "${repo_root}/cloud/hfs/build_runtime_snapshot.sh" "${CLOUDAGENT_RUNTIME_ROOT}" "${CLOUDAGENT_RUNTIME_RELEASE}" >/dev/null
}

expect_start_reject() {
  local label="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    printf 'expected start.sh to reject %s\n' "${label}" >&2
    exit 1
  fi
}

build_runtime_snapshot
expect_start_reject "wrong pinned git SHA" env CLOUDAGENT_RUNTIME_GIT_SHA="$(printf '0%.0s' {1..40})" bash "${repo_root}/cloud/hfs/start.sh"
expect_start_reject "wrong pinned version" env CLOUDAGENT_RUNTIME_VERSION="0.0.0" bash "${repo_root}/cloud/hfs/start.sh"
printf '\n# smoke corruption\n' >> "${runtime_dir}/src/cloudagent_platform/app.py"
expect_start_reject "runtime manifest hash mismatch" bash "${repo_root}/cloud/hfs/start.sh"
build_runtime_snapshot
printf 'print("not listed")\n' > "${runtime_dir}/src/cloudagent_platform/unlisted_runtime_file.py"
expect_start_reject "runtime manifest inventory mismatch" bash "${repo_root}/cloud/hfs/start.sh"
rm "${runtime_dir}/src/cloudagent_platform/unlisted_runtime_file.py"
ln -s app.py "${runtime_dir}/src/cloudagent_platform/runtime_symlink.py"
expect_start_reject "runtime symlink" bash "${repo_root}/cloud/hfs/start.sh"
rm "${runtime_dir}/src/cloudagent_platform/runtime_symlink.py"
build_runtime_snapshot

bash "${repo_root}/cloud/hfs/start.sh" >"${tmp_dir}/server.log" 2>&1 &
server_pid="$!"

if ! python3 - "${port}" "${CLOUDAGENT_AUTH_TOKEN}" "${server_pid}" "${startup_timeout_seconds}" <<'PY'
from __future__ import annotations

import json
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

port, token, server_pid, startup_timeout = sys.argv[1:]
base_url = f"http://127.0.0.1:{port}"

try:
    startup_timeout_seconds = float(startup_timeout)
except ValueError as exc:
    raise SystemExit(
        "CLOUDAGENT_SMOKE_STARTUP_TIMEOUT_SECONDS must be numeric"
    ) from exc
if startup_timeout_seconds <= 0:
    raise SystemExit(
        "CLOUDAGENT_SMOKE_STARTUP_TIMEOUT_SECONDS must be greater than zero"
    )


def request(path: str, *, method: str = "GET", payload: dict | None = None, authenticated: bool = False):
    body = None
    headers: dict[str, str] = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if authenticated:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(base_url + path, data=body, headers=headers, method=method)
    with urlopen(req, timeout=3) as response:
        content_type = response.headers.get("Content-Type", "")
        raw = response.read()
        if "application/json" in content_type:
            return response.status, json.loads(raw)
        return response.status, raw.decode("utf-8")


def process_state(pid: str) -> str | None:
    result = subprocess.run(
        ["ps", "-p", pid, "-o", "stat="],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    state = result.stdout.strip()
    return state or None


started_at = time.monotonic()
deadline = started_at + startup_timeout_seconds
last_error: Exception | None = None
attempts = 0
while time.monotonic() < deadline:
    state = process_state(server_pid)
    if state is None or state.startswith("Z"):
        raise SystemExit(
            f"runtime process exited before becoming healthy: pid={server_pid}, "
            f"state={state or 'missing'}, attempts={attempts}"
        )
    attempts += 1
    try:
        status, health = request("/_ops/healthz")
        if status == 200 and health.get("status") == "ok":
            break
        last_error = RuntimeError(
            f"unexpected health response: status={status}, payload={health!r}"
        )
    except (HTTPError, URLError, TimeoutError, ConnectionError) as exc:
        last_error = exc
    time.sleep(0.25)
else:
    elapsed = time.monotonic() - started_at
    raise SystemExit(
        f"runtime did not become healthy within {startup_timeout_seconds:.1f}s: "
        f"pid={server_pid}, state={process_state(server_pid) or 'missing'}, "
        f"attempts={attempts}, elapsed={elapsed:.1f}s, last_error={last_error}"
    )

status, readiness = request("/_ops/readyz")
assert status == 200, readiness
assert readiness["status"] == "ready", readiness

status, spec = request("/openapi.json")
assert status == 200, spec
assert spec["info"]["version"] == "0.2.0", spec["info"]

status, console = request("/admin")
assert status == 200, console
assert "CloudAgent" in console and "console.js" in console, console[:500]

try:
    request("/api/v1/admin/overview")
except HTTPError as exc:
    assert exc.code == 401, exc
else:
    raise AssertionError("protected overview unexpectedly accepted an anonymous request")

status, bootstrap = request(
    "/api/v1/admin/showcase/bootstrap",
    method="POST",
    payload={},
    authenticated=True,
)
assert status == 200, bootstrap
assert bootstrap["run"]["status"] == "queued", bootstrap

status, overview = request("/api/v1/admin/overview", authenticated=True)
assert status == 200, overview
assert overview["counts"]["agents"] == 1, overview["counts"]
assert overview["signals"]["queue_depth"] == 1, overview["signals"]
PY
then
  printf 'Mounted runtime smoke failed. Diagnostics follow:\n' >&2
  printf 'server_pid=%s port=%s startup_timeout_seconds=%s\n' \
    "${server_pid}" "${port}" "${startup_timeout_seconds}" >&2
  ps -p "${server_pid}" -o pid=,ppid=,stat=,etime=,command= >&2 || true
  if command -v lsof >/dev/null 2>&1; then
    printf '%s\n' 'Listening sockets:' >&2
    lsof -nP -a -p "${server_pid}" -iTCP -sTCP:LISTEN >&2 || true
  fi
  printf '%s\n' 'Server log:' >&2
  sed -n '1,240p' "${tmp_dir}/server.log" >&2
  exit 1
fi

printf 'Mounted runtime smoke passed on port %s\n' "${port}"
