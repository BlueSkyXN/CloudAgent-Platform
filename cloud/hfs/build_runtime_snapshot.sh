#!/usr/bin/env bash
set -euo pipefail

printf '%s\n' \
  'Runtime snapshot generation is retired: CloudAgent HFS uses an immutable source build.' \
  'Export the source wrapper with cloud/hfs/export_space_bundle.sh instead.' >&2
exit 64
