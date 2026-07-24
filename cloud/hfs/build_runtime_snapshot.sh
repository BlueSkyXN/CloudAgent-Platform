#!/usr/bin/env bash
set -euo pipefail

repo_root="${CLOUDAGENT_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
repo_root="$(cd "${repo_root}" && pwd)"
snapshot_root="${1:?usage: build_runtime_snapshot.sh SNAPSHOT_ROOT [RELEASE_ID]}"

git_sha="$(git -C "${repo_root}" rev-parse --verify HEAD)"
version="$(PYTHONPATH="${repo_root}/src" python3 -c 'from cloudagent_platform import __version__; print(__version__)')"
expected_release_id="v${version}-${git_sha}"
release_id="${2:-${expected_release_id}}"

if ! [[ "${release_id}" =~ ^v[0-9A-Za-z._-]+-[0-9a-f]{40}$ ]]; then
  printf 'release id must be v<version>-<40-character-git-sha>; got %s\n' "${release_id}" >&2
  exit 64
fi
if [[ "${release_id}" != "${expected_release_id}" ]]; then
  printf 'release id must equal %s for this source tree; got %s\n' "${expected_release_id}" "${release_id}" >&2
  exit 64
fi

git_dirty=false
if [[ -n "$(git -C "${repo_root}" status --porcelain)" ]]; then
  git_dirty=true
  if [[ "${CLOUDAGENT_ALLOW_DIRTY_SNAPSHOT:-false}" != "true" ]]; then
    printf 'refusing runtime snapshot from dirty working tree; commit or stash changes first\n' >&2
    exit 65
  fi
  printf 'warning: building a non-release snapshot from a dirty working tree; start.sh will reject it\n' >&2
fi

release_dir="${snapshot_root%/}/releases/${release_id}"
if [[ -e "${release_dir}" ]]; then
  printf 'release directory already exists: %s\n' "${release_dir}" >&2
  exit 66
fi
mkdir -p "${release_dir}"

cleanup() {
  rm -rf "${release_dir}"
}
trap cleanup ERR

# Archive the committed tree so generated files and source-tree residue cannot
# silently enter an immutable runtime release.
git -C "${repo_root}" archive --format=tar "${git_sha}" -- src/cloudagent_platform pyproject.toml | tar -xf - -C "${release_dir}"

PYTHONPATH="${release_dir}/src" python3 - "${release_dir}" "${release_id}" "${version}" "${git_sha}" "${git_dirty}" <<'PY'
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

release_dir = Path(sys.argv[1])
release_id, version, git_sha, git_dirty = sys.argv[2:]
files: list[dict[str, object]] = []
for path in sorted(release_dir.rglob("*")):
    if path.is_symlink():
        raise SystemExit(f"runtime snapshot must not contain symlinks: {path.relative_to(release_dir)}")
    if not path.is_file():
        continue
    relative = path.relative_to(release_dir).as_posix()
    if relative == "RUNTIME_MANIFEST.json":
        continue
    if "__pycache__" in path.parts or path.suffix == ".pyc":
        raise SystemExit(f"forbidden generated file in runtime snapshot: {relative}")
    files.append(
        {
            "path": relative,
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    )

manifest = {
    "schema_version": 2,
    "release_id": release_id,
    "version": version,
    "git_sha": git_sha,
    "git_dirty": git_dirty == "true",
    "files": files,
}
release_dir.joinpath("RUNTIME_MANIFEST.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY

trap - ERR
printf 'Runtime snapshot created: %s\n' "${release_dir}"
printf 'release_id=%s\nversion=%s\ngit_sha=%s\n' "${release_id}" "${version}" "${git_sha}"
