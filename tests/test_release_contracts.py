from __future__ import annotations

import json
import os
import subprocess
import tempfile
import tomllib
import unittest
import importlib.util
from pathlib import Path

from cloudagent_platform.openapi import current_openapi


REPO_ROOT = Path(__file__).resolve().parents[1]
HFS_ROOT = REPO_ROOT / "cloud" / "hfs"
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}

_PERSISTENT_PATH_SPEC = importlib.util.spec_from_file_location(
    "validate_persistent_path", HFS_ROOT / "validate_persistent_path.py"
)
assert _PERSISTENT_PATH_SPEC and _PERSISTENT_PATH_SPEC.loader
_PERSISTENT_PATH = importlib.util.module_from_spec(_PERSISTENT_PATH_SPEC)
_PERSISTENT_PATH_SPEC.loader.exec_module(_PERSISTENT_PATH)


class OpenAPIReleaseContractTests(unittest.TestCase):
    def test_each_operation_has_stable_contract_basics(self) -> None:
        spec = current_openapi()
        self.assertEqual(spec["openapi"], "3.1.0")
        operation_ids: set[str] = set()
        for path, path_item in spec["paths"].items():
            expected_parameters = {
                segment[1:-1]
                for segment in path.split("/")
                if segment.startswith("{") and segment.endswith("}")
            }
            defined_parameters = {
                parameter["name"]
                for parameter in path_item.get("parameters", [])
                if parameter.get("in") == "path"
            }
            self.assertEqual(expected_parameters, defined_parameters, path)
            for method, operation in path_item.items():
                if method not in HTTP_METHODS:
                    continue
                self.assertIn("operationId", operation, f"{method.upper()} {path}")
                self.assertNotIn(operation["operationId"], operation_ids)
                operation_ids.add(operation["operationId"])
                responses = operation.get("responses")
                self.assertIsInstance(responses, dict, f"{method.upper()} {path}")
                self.assertTrue(responses, f"{method.upper()} {path}")
                self.assertTrue(
                    any(str(status).startswith("2") for status in responses),
                    f"{method.upper()} {path} lacks a success response",
                )
                self.assertTrue(
                    any(str(status).startswith("4") or status == "default" for status in responses),
                    f"{method.upper()} {path} lacks an error response",
                )

        events = spec["paths"]["/api/v1/sessions/{session_id}/events"]["post"]
        self.assertTrue({"201", "202"}.issubset(events["responses"]))
        for suffix in ("events", "artifacts", "usage"):
            operation = spec["paths"][f"/api/v1/workers/{{worker_id}}/runs/{{run_id}}/{suffix}"]["post"]
            self.assertIn("201", operation["responses"])
        stream = spec["paths"]["/api/v1/sessions/{session_id}/events/stream"]["get"]
        self.assertIn("text/event-stream", stream["responses"]["200"]["content"])
        for path in ("/api/v1/files/{file_id}/content", "/api/v1/artifacts/{artifact_id}/content"):
            self.assertIn(
                "application/octet-stream",
                spec["paths"][path]["get"]["responses"]["200"]["content"],
            )
        session_workspace = spec["paths"][
            "/api/v1/admin/sessions/{session_id}/workspace"
        ]["get"]
        self.assertEqual(
            session_workspace["responses"]["200"]["content"]["application/json"][
                "schema"
            ]["$ref"],
            "#/components/schemas/SessionWorkspace",
        )

    def test_local_references_resolve(self) -> None:
        spec = current_openapi()

        def walk(value: object) -> None:
            if isinstance(value, dict):
                reference = value.get("$ref")
                if isinstance(reference, str) and reference.startswith("#/"):
                    target: object = spec
                    for part in reference[2:].split("/"):
                        self.assertIsInstance(target, dict, reference)
                        target = target[part]
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(spec)

    def test_webhook_console_remains_credential_configurable(self) -> None:
        console = (REPO_ROOT / "src/cloudagent_platform/web/console.js").read_text(encoding="utf-8")
        self.assertIn('const inboundWebhook = i.provider === "webhook"', console)
        self.assertIn("const canRegister = inboundWebhook || Boolean(i.base_url)", console)
        self.assertIn("Webhook is inbound-only and does not use one", console)

    def test_hfs_registry_is_source_lane_and_registers_only_real_setting_names(self) -> None:
        manifest = tomllib.loads((HFS_ROOT / "hfs-dev.toml").read_text(encoding="utf-8"))
        self.assertEqual(manifest["standard"], "2.0")
        self.assertEqual(manifest["lane"], "source")
        self.assertEqual(manifest["version_source"], "commit")
        self.assertEqual(manifest["secrets"], ["CLOUDAGENT_AUTH_TOKEN"])
        self.assertEqual(manifest["optional_secrets"], [])
        self.assertEqual(manifest["variables"], [])

    def test_hfs_formal_workflow_is_exact_main_private_and_read_back(self) -> None:
        workflow = (REPO_ROOT / ".github/workflows/deploy-hf-space.yml").read_text(
            encoding="utf-8"
        )
        for required in (
            "FORMAL_SPACE: BlueSkyXN/cloudagent-platform-hfs",
            "[[ \"$GITHUB_REF\" == refs/heads/main ]]",
            "[[ \"$(git rev-parse origin/main)\" == \"$SOURCE_REF\" ]]",
            "canonical Space must already exist and be private",
            'huggingface_hub==${HF_CLI_VERSION}',
            "python3 -m huggingface_hub.cli.hf --help",
            "canonical repository path readback does not match",
            'runtime.raw.get("sha") == deployed_revision',
        ):
            self.assertIn(required, workflow)

    def test_hfs_exporter_declares_and_enforces_a_flat_allowlist(self) -> None:
        exporter = (HFS_ROOT / "export_space_bundle.sh").read_text(encoding="utf-8")
        for required in (
            "expected_export_files=(",
            "git -C \"${repo_root}\" ls-files --error-unmatch",
            "export source must be a tracked regular file",
            "unexpected file in Space export",
        ):
            self.assertIn(required, exporter)

    def test_hfs_source_wrapper_static_contract(self) -> None:
        result = subprocess.run(
            ["python3", str(HFS_ROOT / "validate_source_wrapper.py")],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("HFS source-wrapper contract passed", result.stdout)

    def test_exporter_refuses_to_overwrite_an_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                ["bash", str(HFS_ROOT / "export_space_bundle.sh"), temporary],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing existing export target", result.stderr)

    def test_exporter_has_no_dirty_provenance_bypass(self) -> None:
        exporter = (HFS_ROOT / "export_space_bundle.sh").read_text(encoding="utf-8")
        validator = (HFS_ROOT / "validate_source_wrapper.py").read_text(encoding="utf-8")
        self.assertNotIn("CLOUDAGENT_ALLOW_DIRTY_EXPORT", exporter)
        self.assertNotIn("allow-dirty-export", validator)

    def test_persistent_database_rejects_any_symlink_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data" / "cloudagent"
            outside = Path(temporary) / "outside"
            root.mkdir(parents=True)
            outside.mkdir()
            (root / "nested").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symlink component"):
                _PERSISTENT_PATH.validate(root / "nested" / "cloudagent.sqlite3", root)

    def test_retired_snapshot_builder_fails_closed(self) -> None:
        result = subprocess.run(
            ["bash", str(HFS_ROOT / "build_runtime_snapshot.sh"), "/not-used"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 64)
        self.assertIn("retired", result.stderr)

    def test_hfs_provenance_placeholders_remain_source_contracts(self) -> None:
        build_source = dict(
            line.split("=", 1)
            for line in (HFS_ROOT / "BUILD_SOURCE.txt").read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        bundle = json.loads((HFS_ROOT / "BUNDLE_MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(build_source["lane"], "source")
        self.assertEqual(build_source["version_source"], "commit")
        self.assertRegex(build_source["source_commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(bundle["schema_version"], 2)
        self.assertEqual(bundle["source_commit"], build_source["source_commit"])


if __name__ == "__main__":
    unittest.main()
