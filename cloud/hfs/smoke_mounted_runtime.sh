#!/usr/bin/env bash
set -euo pipefail

printf '%s\n' \
  'The historical mounted-runtime smoke command now validates the source-wrapper contract.' \
  'Use smoke_source_wrapper.sh for the canonical command name.' >&2
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
exec bash "${repo_root}/cloud/hfs/smoke_source_wrapper.sh" "$@"
