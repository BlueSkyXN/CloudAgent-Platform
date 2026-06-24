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
PY

rm -rf "${out_dir}"
mkdir -p "${out_dir}"

copy_file() {
  local name="$1"
  cp "${hfs_dir}/${name}" "${out_dir}/${name}"
}

copy_file README.md
copy_file Dockerfile
copy_file .dockerignore
copy_file app.py
copy_file start.sh
copy_file healthcheck.sh
copy_file hfs-dev.toml

cat > "${out_dir}/BUILD_SOURCE.txt" <<EOT
source_repo=https://github.com/BlueSkyXN/CloudAgent-Platform.git
source_path=cloud/hfs
bundle_generated_at=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
mode=bucket-mounted-runtime
runtime_bucket=BlueSkyXN/cloudagent-platform-hfs-runtime
runtime_mount=/mnt/cloudagent-runtime
EOT

python3 - "${out_dir}" "${repo_root}" <<'PY'
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

out_dir = Path(sys.argv[1])
repo_root = Path(sys.argv[2])


def git_value(*args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo_root), *args],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except subprocess.CalledProcessError:
        return None


files = []
for path in sorted(out_dir.rglob("*")):
    if not path.is_file() or path.name == "BUNDLE_MANIFEST.json":
        continue
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    files.append(
        {
            "path": path.relative_to(out_dir).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": digest,
        }
    )

status = git_value("status", "--short")
manifest = {
    "schema_version": 1,
    "mode": "bucket-mounted-runtime",
    "source_repo": "https://github.com/BlueSkyXN/CloudAgent-Platform.git",
    "source_path": "cloud/hfs",
    "runtime_bucket": "BlueSkyXN/cloudagent-platform-hfs-runtime",
    "runtime_mount": "/mnt/cloudagent-runtime",
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "git_sha": git_value("rev-parse", "--verify", "HEAD"),
    "git_dirty": bool(status),
    "files": files,
    "forbidden_paths": [".git", ".env", ".env.local", "local", "data", "logs"],
}
out_dir.joinpath("BUNDLE_MANIFEST.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY

cat > "${out_dir}/.gitignore" <<'EOF'
.DS_Store
.env
.env.*
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

for forbidden in .git .env .env.local local data logs dist node_modules; do
  if [[ -e "${out_dir}/${forbidden}" ]]; then
    printf 'Forbidden export path exists: %s\n' "${forbidden}" >&2
    exit 3
  fi
done

if find "${out_dir}" \( -name '*.sqlite' -o -name '*.sqlite3' -o -name '*.log' \) | grep -q .; then
  printf 'Forbidden runtime artifact detected in export\n' >&2
  exit 3
fi

printf 'HF Space bundle exported to %s\n' "${out_dir}"
