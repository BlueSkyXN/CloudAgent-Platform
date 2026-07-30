#!/usr/bin/env python3
"""Validate CloudAgent's local HFS v2.1 source-wrapper contract without network access."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
HFS_ROOT = REPO_ROOT / "cloud" / "hfs"
EXPECTED_EXPORTED_FILES = {
    ".dockerignore",
    ".gitignore",
    "BUILD_SOURCE.txt",
    "BUNDLE_MANIFEST.json",
    "Dockerfile",
    "README.md",
    "healthcheck.sh",
    "hfs-dev.toml",
    "start.sh",
    "validate_persistent_path.py",
}
FORBIDDEN_EXPORT_NAMES = {".git", ".env", "local", "data", "logs", "src", "app.py"}
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def fail(message: str) -> None:
    raise AssertionError(message)


def parse_key_values(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or key in result:
            fail(f"invalid provenance line in {path.name}: {line!r}")
        result[key] = value
    return result


def current_commit() -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    commit = result.stdout.strip()
    if not COMMIT_SHA.fullmatch(commit):
        fail(f"Git HEAD is not a full lowercase commit SHA: {commit!r}")
    return commit


def check_registry() -> None:
    manifest = tomllib.loads((HFS_ROOT / "hfs-dev.toml").read_text(encoding="utf-8"))
    candidate = tomllib.loads((HFS_ROOT / "hfs-dev.candidate.toml").read_text(encoding="utf-8"))
    expected = {
        "standard": "2.1",
        "project": "cloudagent-platform",
        "space": "BlueSkyXN/cloudagent-platform-hfs",
        "sovereignty": "sovereign",
        "lane": "source",
        "version_source": "commit",
        "project_class": "preview",
        "target_role": "primary",
        "env_file": ".env",
        "secret_files": [],
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            fail(f"hfs-dev.toml {key} must be {value!r}")
    candidate_expected = {
        "space": "BlueSkyXN/cloudagent-platform-hfs-v2-candidate",
        "target_role": "candidate",
        "env_file": "local/hfs-targets/candidate.env",
    }
    for key, value in candidate_expected.items():
        if candidate.get(key) != value:
            fail(f"candidate manifest {key} must be {value!r}")
    for key in sorted(set(manifest) | set(candidate)):
        if (
            key not in {"space", "target_role", "env_file"}
            and manifest.get(key) != candidate.get(key)
        ):
            fail(f"candidate manifest differs from canonical preview at {key}")
    if manifest.get("secrets") != ["CLOUDAGENT_AUTH_TOKEN"]:
        fail("hfs-dev.toml must register only CLOUDAGENT_AUTH_TOKEN as a Space Secret")
    if manifest.get("optional_secrets") != []:
        fail("hfs-dev.toml optional_secrets must be present and empty")
    if manifest.get("variables") != []:
        fail("hfs-dev.toml must not invent Space Variables for source provenance")


def check_source_files() -> None:
    dockerfile = (HFS_ROOT / "Dockerfile").read_text(encoding="utf-8")
    for required in (
        "ARG CLOUDAGENT_SOURCE_REF=",
        "remote add origin https://github.com/BlueSkyXN/CloudAgent-Platform.git",
        "fetch --depth=1 origin",
        "checkout --detach FETCH_HEAD",
        "git -C \"${CLOUDAGENT_SOURCE_ROOT}\" rev-parse HEAD",
        "CLOUDAGENT_SOURCE_REF must be a full lowercase Git commit SHA",
    ):
        if required not in dockerfile:
            fail(f"Dockerfile is missing source provenance guard: {required}")
    source_ref = re.search(r"(?m)^ARG CLOUDAGENT_SOURCE_REF=([0-9a-f]{40})$", dockerfile)
    if source_ref is None:
        fail("Dockerfile template must carry a full immutable source SHA")
    if "CLOUDAGENT_SOURCE_REPOSITORY" in dockerfile:
        fail("Dockerfile must not allow a build argument to replace the canonical source repository")

    start = (HFS_ROOT / "start.sh").read_text(encoding="utf-8")
    for required in (
        "CLOUDAGENT_AUTH_TOKEN must be configured",
        "persistent SQLite path contains a symlink component after initialization",
        "persistent SQLite database path must not be a symlink",
        "SQLite persistence readiness check failed",
        "checked-out source Git HEAD does not match CLOUDAGENT_SOURCE_REF",
    ):
        if required not in start:
            fail(f"start.sh is missing fail-closed guard: {required}")
    for forbidden in ("CLOUDAGENT_RUNTIME_", "/mnt/cloudagent-runtime", "CLOUDAGENT_HFS_ALLOW_PROBE_FALLBACK"):
        if forbidden in start:
            fail(f"start.sh contains retired runtime-mount fallback: {forbidden}")

    healthcheck = (HFS_ROOT / "healthcheck.sh").read_text(encoding="utf-8")
    if "/_ops/healthz" not in healthcheck or "urlopen" not in healthcheck:
        fail("healthcheck.sh must make a local health request")
    if "app.py" in healthcheck:
        fail("healthcheck.sh must not launch a probe server")

    persistent_validator = HFS_ROOT / "validate_persistent_path.py"
    if not persistent_validator.is_file():
        fail("validate_persistent_path.py must validate every persistence path component")
    persistent_source = persistent_validator.read_text(encoding="utf-8")
    for required in (
        "CLOUDAGENT_DB resolves outside /data/cloudagent",
        "CLOUDAGENT_DB must not be a symlink",
        "persistent path contains a symlink component",
    ):
        if required not in persistent_source:
            fail(f"validate_persistent_path.py is missing guard: {required}")

    retired_probe = (HFS_ROOT / "app.py").read_text(encoding="utf-8")
    if "retired" not in retired_probe.lower() or "ThreadingHTTPServer" in retired_probe:
        fail("legacy deployment probe must remain retired and non-runnable")


def check_export() -> None:
    source_commit = current_commit()
    with tempfile.TemporaryDirectory(prefix="cloudagent-hfs-contract-") as temporary:
        out_dir = Path(temporary) / "space"
        result = subprocess.run(
            ["bash", str(HFS_ROOT / "export_space_bundle.sh"), str(out_dir)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            fail(f"Space export failed: {result.stdout}{result.stderr}")

        exported_files = {path.name for path in out_dir.iterdir()}
        if exported_files != EXPECTED_EXPORTED_FILES:
            fail(f"unexpected flat Space root: {sorted(exported_files)!r}")
        for path in out_dir.rglob("*"):
            relative = path.relative_to(out_dir)
            if path.is_symlink():
                fail(f"Space root contains symlink: {relative}")
            if any(part in FORBIDDEN_EXPORT_NAMES or part.startswith(".env") for part in relative.parts):
                fail(f"Space root contains forbidden path: {relative}")

        provenance = parse_key_values(out_dir / "BUILD_SOURCE.txt")
        expected_provenance = {
            "contract_schema": "2",
            "lane": "source",
            "version_source": "commit",
            "source_repo": "https://github.com/BlueSkyXN/CloudAgent-Platform.git",
            "source_commit": source_commit,
            "wrapper_source_path": "cloud/hfs",
        }
        for key, value in expected_provenance.items():
            if provenance.get(key) != value:
                fail(f"BUILD_SOURCE.txt {key} does not match source contract")

        bundle = json.loads((out_dir / "BUNDLE_MANIFEST.json").read_text(encoding="utf-8"))
        for key, value in expected_provenance.items():
            bundle_key = "schema_version" if key == "contract_schema" else key
            expected_value: object = 2 if key == "contract_schema" else value
            if bundle.get(bundle_key) != expected_value:
                fail(f"BUNDLE_MANIFEST.json {bundle_key} does not match source contract")
        listed = {entry["path"]: entry for entry in bundle.get("files", [])}
        expected_listed = exported_files - {"BUNDLE_MANIFEST.json"}
        if set(listed) != expected_listed:
            fail("BUNDLE_MANIFEST.json file inventory does not match flat export")
        for name, entry in listed.items():
            payload = (out_dir / name).read_bytes()
            if entry.get("bytes") != len(payload) or entry.get("sha256") != hashlib.sha256(payload).hexdigest():
                fail(f"BUNDLE_MANIFEST.json digest mismatch: {name}")

        exported_dockerfile = (out_dir / "Dockerfile").read_text(encoding="utf-8")
        if f"ARG CLOUDAGENT_SOURCE_REF={source_commit}" not in exported_dockerfile:
            fail("exported Dockerfile does not pin the current immutable source commit")
        for forbidden in ("bucket-mounted-runtime", "CLOUDAGENT_RUNTIME_", "/mnt/cloudagent-runtime"):
            if forbidden in "\n".join(path.read_text(encoding="utf-8") for path in out_dir.iterdir() if path.is_file()):
                fail(f"exported wrapper retains a retired runtime-mount dependency: {forbidden}")

        candidate_dir = Path(temporary) / "candidate-space"
        candidate_env = os.environ.copy()
        candidate_env["HFS_MANIFEST"] = "hfs-dev.candidate.toml"
        candidate_result = subprocess.run(
            ["bash", str(HFS_ROOT / "export_space_bundle.sh"), str(candidate_dir)],
            cwd=REPO_ROOT,
            env=candidate_env,
            capture_output=True,
            text=True,
        )
        if candidate_result.returncode != 0:
            fail(f"candidate Space export failed: {candidate_result.stdout}{candidate_result.stderr}")
        if {path.name for path in candidate_dir.iterdir()} != EXPECTED_EXPORTED_FILES:
            fail("candidate export does not match the flat wrapper allowlist")
        candidate_manifest = tomllib.loads((candidate_dir / "hfs-dev.toml").read_text(encoding="utf-8"))
        if candidate_manifest.get("space") != "BlueSkyXN/cloudagent-platform-hfs-v2-candidate":
            fail("candidate export did not select hfs-dev.candidate.toml")
        candidate_provenance = parse_key_values(candidate_dir / "BUILD_SOURCE.txt")
        if candidate_provenance.get("source_commit") != source_commit:
            fail("candidate export provenance does not match the wrapper commit")


def check_formal_workflow() -> None:
    path = REPO_ROOT / ".github/workflows/deploy-hf-space.yml"
    if not path.is_file():
        fail("canonical formal deployment workflow is missing")
    source = path.read_text(encoding="utf-8")
    for required in (
        "FORMAL_SPACE: BlueSkyXN/cloudagent-platform-hfs",
        "environment: hfs-production",
        "PUBLISH_FORMAL",
        "validate_source_wrapper.py",
        "canonical repository path readback does not match",
        'HF_CLI_CLICK_VERSION: "8.3.3"',
        "huggingface_hub==${HF_CLI_VERSION}",
        "click==${HF_CLI_CLICK_VERSION}",
        "python3 -m huggingface_hub.cli.hf --help",
        'runtime.raw.get("sha") == deployed_revision',
    ):
        if required not in source:
            fail(f"formal deployment workflow is missing guard: {required}")


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        check_registry()
        check_source_files()
        check_export()
        check_formal_workflow()
    except (AssertionError, OSError, subprocess.CalledProcessError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"HFS source-wrapper contract failed: {exc}", file=sys.stderr)
        return 1
    print("HFS source-wrapper contract passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
