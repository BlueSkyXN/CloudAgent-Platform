from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from cloudagent_platform.openapi import current_openapi


REPO_ROOT = Path(__file__).resolve().parents[1]
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


class OpenAPIReleaseContractTests(unittest.TestCase):
    def clone_with_current_hfs_scripts(self, directory: str) -> Path:
        clone = Path(directory) / "clone"
        subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(REPO_ROOT), str(clone)],
            check=True,
        )
        for name in ("export_space_bundle.sh", "build_runtime_snapshot.sh"):
            shutil.copy2(
                REPO_ROOT / "cloud/hfs" / name,
                clone / "cloud/hfs" / name,
            )
        return clone

    def test_each_operation_has_stable_contract_basics(self) -> None:
        spec = current_openapi()
        self.assertEqual(spec["openapi"], "3.1.0")
        operation_ids: set[str] = set()
        for path, path_item in spec["paths"].items():
            expected_parameters = set(re.findall(r"\{([^}]+)\}", path))
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

    def test_local_references_resolve(self) -> None:
        spec = current_openapi()
        root = spec

        def walk(value: object) -> None:
            if isinstance(value, dict):
                reference = value.get("$ref")
                if isinstance(reference, str) and reference.startswith("#/"):
                    target: object = root
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

    def test_export_rejects_a_dirty_tree_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clone = self.clone_with_current_hfs_scripts(directory)
            readme = clone / "README.md"
            readme.write_text(readme.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            result = subprocess.run(
                ["bash", "cloud/hfs/export_space_bundle.sh", str(Path(directory) / "bundle")],
                cwd=clone,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 65, result.stdout + result.stderr)
            self.assertIn("refusing Space export from dirty working tree", result.stderr)

    def test_runtime_snapshot_builder_rejects_a_dirty_tree_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clone = self.clone_with_current_hfs_scripts(directory)
            readme = clone / "README.md"
            readme.write_text(readme.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            result = subprocess.run(
                ["bash", "cloud/hfs/build_runtime_snapshot.sh", str(Path(directory) / "runtime")],
                cwd=clone,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 65, result.stdout + result.stderr)
            self.assertIn("refusing runtime snapshot from dirty working tree", result.stderr)

    def test_runtime_snapshot_builder_rejects_a_release_id_with_the_wrong_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clone = self.clone_with_current_hfs_scripts(directory)
            git_sha = subprocess.check_output(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=clone,
                text=True,
            ).strip()
            result = subprocess.run(
                [
                    "bash",
                    "cloud/hfs/build_runtime_snapshot.sh",
                    str(Path(directory) / "runtime"),
                    f"v9.9.9-{git_sha}",
                ],
                cwd=clone,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 64, result.stdout + result.stderr)
            self.assertIn("release id must equal", result.stderr)


if __name__ == "__main__":
    unittest.main()
