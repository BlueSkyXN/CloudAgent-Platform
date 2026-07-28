#!/usr/bin/env python3
"""Fail closed when a SQLite persistence path traverses a symlink."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def validate(database: Path, data_root: Path) -> Path:
    if not database.is_absolute() or not data_root.is_absolute():
        raise ValueError("database and data root must be absolute paths")
    if database.name in {"", ".", ".."}:
        raise ValueError("CLOUDAGENT_DB must name a SQLite file below /data/cloudagent")
    try:
        relative = database.relative_to(data_root)
    except ValueError as exc:
        raise ValueError("CLOUDAGENT_DB resolves outside /data/cloudagent") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("CLOUDAGENT_DB must name a SQLite file below /data/cloudagent")

    # Do not call resolve() until every existing component has been checked:
    # resolve() would otherwise make a symlink escaping the persistent root look
    # like an allowed canonical path.
    current = data_root
    for part in relative.parts[:-1]:
        if current.is_symlink():
            raise ValueError(f"persistent path contains a symlink component: {current}")
        current /= part
    if current.is_symlink():
        raise ValueError(f"persistent path contains a symlink component: {current}")
    if database.is_symlink():
        raise ValueError("CLOUDAGENT_DB must not be a symlink")
    return database


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--data-root", type=Path, default=Path("/data/cloudagent"))
    args = parser.parse_args()
    try:
        print(validate(args.database, args.data_root))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 65
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
