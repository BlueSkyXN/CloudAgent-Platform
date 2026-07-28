#!/usr/bin/env bash
set -euo pipefail

port="${PORT:-7860}"
python3 - "${port}" <<'PY'
from __future__ import annotations

import json
import sys
from urllib.request import urlopen

port = sys.argv[1]
with urlopen(f"http://127.0.0.1:{port}/_ops/healthz", timeout=3) as response:
    if response.status != 200:
        raise SystemExit(f"unexpected health status: {response.status}")
    payload = json.loads(response.read().decode("utf-8"))
    if payload.get("status") != "ok":
        raise SystemExit(f"unexpected health payload: {payload!r}")
PY
