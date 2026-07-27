#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
hfs_dir="${repo_root}/cloud/hfs"
requested_out_dir="${1:-${TMPDIR:-/tmp}/cloudagent-platform-hfs-space}"

canonical_path() {
  python3 - "$1" <<'PY'
from pathlib import Path
import sys

target = Path(sys.argv[1]).expanduser()
parent = target.parent.resolve()
print((parent / target.name).as_posix())
PY
}

out_dir="$(canonical_path "${requested_out_dir}")"
repo_root="$(canonical_path "${repo_root}")"
hfs_dir="$(canonical_path "${hfs_dir}")"
source_commit="$(git -C "${repo_root}" rev-parse --verify HEAD)"

if ! [[ "${source_commit}" =~ ^[0-9a-f]{40}$ ]]; then
  printf 'current Git HEAD is not a full lowercase commit SHA\n' >&2
  exit 64
fi

if [[ -n "$(git -C "${repo_root}" status --porcelain)" ]]; then
  printf 'refusing Space export from dirty working tree; commit changes first\n' >&2
  exit 65
fi

python3 - "${out_dir}" "${repo_root}" "${hfs_dir}" <<'PY'
from pathlib import Path
import sys

out_dir = Path(sys.argv[1])
repo_root = Path(sys.argv[2])
hfs_dir = Path(sys.argv[3])
home = Path.home().resolve()
unsafe_exact = {
    Path("/"),
    Path("/tmp").resolve(),
    Path("/var").resolve(),
    Path("/Users").resolve(),
    home,
    repo_root,
    hfs_dir,
}
if out_dir in unsafe_exact:
    raise SystemExit(f"Refusing unsafe export target: {out_dir}")
if repo_root in out_dir.parents or hfs_dir in out_dir.parents:
    raise SystemExit(f"Refusing export target inside source tree: {out_dir}")
if out_dir in repo_root.parents or out_dir in hfs_dir.parents:
    raise SystemExit(f"Refusing export target that contains source tree: {out_dir}")
if out_dir.exists():
    raise SystemExit(f"Refusing existing export target; choose a new empty path: {out_dir}")
PY

bundle_dir="$(mktemp -d "${out_dir}.tmp.XXXXXX")"
cleanup() {
  rm -rf -- "${bundle_dir}"
}
trap cleanup EXIT

copy_file() {
  local name="$1"
  cp "${hfs_dir}/${name}" "${bundle_dir}/${name}"
}

copy_file README.md
copy_file Dockerfile
copy_file .dockerignore
copy_file start.sh
copy_file healthcheck.sh
copy_file hfs-dev.toml
copy_file validate_persistent_path.py

python3 - "${bundle_dir}/Dockerfile" "${source_commit}" <<'PY'
from __future__ import annotations

import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
source_commit = sys.argv[2]
content = path.read_text(encoding="utf-8")
updated, count = re.subn(
    r"(?m)^ARG CLOUDAGENT_SOURCE_REF=[0-9a-f]{40}$",
    f"ARG CLOUDAGENT_SOURCE_REF={source_commit}",
    content,
)
if count != 1:
    raise SystemExit("Dockerfile must contain exactly one full CLOUDAGENT_SOURCE_REF build argument")
path.write_text(updated, encoding="utf-8")
PY

cat > "${bundle_dir}/BUILD_SOURCE.txt" <<EOT
contract_schema=2
lane=source
version_source=commit
source_repo=https://github.com/BlueSkyXN/CloudAgent-Platform.git
source_commit=${source_commit}
wrapper_source_path=cloud/hfs
bundle_generated_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
EOT

cat > "${bundle_dir}/.gitignore" <<'EOF'
.DS_Store
.env
.env.*
!.env.example
!.env.sample
!.env.template
local/
data/
logs/
dist/
node_modules/
__pycache__/
*.pyc
*.sqlite
*.sqlite3
*.log
EOF

python3 - "${bundle_dir}" "${source_commit}" <<'PY'
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

out_dir = Path(sys.argv[1])
source_commit = sys.argv[2]
files = []
for path in sorted(out_dir.rglob("*")):
    if path.is_symlink():
        raise SystemExit(f"Space export must not contain symlinks: {path.relative_to(out_dir)}")
    if not path.is_file() or path.name == "BUNDLE_MANIFEST.json":
        continue
    files.append(
        {
            "path": path.relative_to(out_dir).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    )

manifest = {
    "schema_version": 2,
    "lane": "source",
    "version_source": "commit",
    "source_repo": "https://github.com/BlueSkyXN/CloudAgent-Platform.git",
    "source_commit": source_commit,
    "wrapper_source_path": "cloud/hfs",
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "files": files,
    "forbidden_paths": [".git", ".env*", "local", "data", "logs", "src"],
}
out_dir.joinpath("BUNDLE_MANIFEST.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY

for forbidden in .git .env .env.local local data logs dist node_modules src app.py; do
  if [[ -e "${bundle_dir}/${forbidden}" ]]; then
    printf 'Forbidden export path exists: %s\n' "${forbidden}" >&2
    exit 3
  fi
done

if find "${bundle_dir}" \( -name '.env' -o -name '.env.*' -o -name '*.sqlite' -o -name '*.sqlite3' -o -name '*.log' \) -print -quit | grep -q .; then
  printf 'Forbidden local configuration or runtime artifact detected in export\n' >&2
  exit 3
fi

mv "${bundle_dir}" "${out_dir}"
trap - EXIT
printf 'HF Space source wrapper exported to %s\n' "${out_dir}"
