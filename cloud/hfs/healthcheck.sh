#!/usr/bin/env bash
set -euo pipefail

port="${PORT:-7860}"
python - "${port}" <<'PY'
from __future__ import annotations

import sys
from urllib.request import urlopen

port = sys.argv[1]
with urlopen(f"http://127.0.0.1:{port}/_ops/healthz", timeout=3) as response:
    if response.status != 200:
        raise SystemExit(1)
PY
