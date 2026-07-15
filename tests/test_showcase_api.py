from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.client import HTTPResponse
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cloudagent_platform.app import Runtime, Store, make_handler


class ShowcaseApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(Store(str(Path(self.tmp.name) / "showcase.sqlite3")), "test-token")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.runtime))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.runtime.stop()
        self.tmp.cleanup()

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        token: str | None = "test-token",
    ) -> tuple[int, dict | str, HTTPResponse | HTTPError]:
        headers: dict[str, str] = {}
        body = None
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as exc:
            response = exc
        raw = response.read().decode("utf-8")
        try:
            decoded: dict | str = json.loads(raw)
        except json.JSONDecodeError:
            decoded = raw
        return response.status, decoded, response

    def test_console_assets_have_safe_headers_and_no_token(self) -> None:
        assets: dict[str, str] = {}
        for path, expected_type, expected_cache in (
            ("/", "text/html; charset=utf-8", "no-store"),
            ("/admin", "text/html; charset=utf-8", "no-store"),
            ("/admin/assets/console.css", "text/css; charset=utf-8", "no-cache"),
            ("/admin/assets/console.js", "application/javascript; charset=utf-8", "no-cache"),
            ("/admin/assets/mark.svg", "image/svg+xml", "no-cache"),
        ):
            status, body, response = self.request("GET", path, token=None)
            self.assertEqual(status, 200)
            self.assertIsInstance(body, str)
            self.assertEqual(response.headers["Content-Type"], expected_type)
            self.assertEqual(response.headers["Cache-Control"], expected_cache)
            self.assertNotIn("test-token", body)
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertIn("camera=()", response.headers["Permissions-Policy"])
            csp = response.headers["Content-Security-Policy"]
            self.assertIn("default-src 'self'", csp)
            self.assertIn("script-src 'self'", csp)
            self.assertIn("style-src 'self'", csp)
            self.assertIn("connect-src 'self'", csp)
            assets[path] = body

        console = assets["/admin/assets/console.js"]
        for contract in (
            "loadEpoch",
            "setBackgroundInert",
            "data-artifact-download",
            "/api/v1/artifacts/",
            "/credential",
            "Submit tool request",
            "Queue run",
        ):
            self.assertIn(contract, console)
        self.assertIn('<main id="main" class="gate">', console)
        self.assertIn('<main id="main" class="workspace">', console)
        self.assertNotIn('data-job-action="trigger"', console)
        console_css = assets["/admin/assets/console.css"]
        self.assertIn("[hidden]", console_css)
        self.assertIn("display: none !important", console_css)
        self.assertIn("body.dialog-open", console_css)

    def test_bootstrap_is_idempotent_and_overview_is_derived(self) -> None:
        status, first, _ = self.request("POST", "/api/v1/admin/showcase/bootstrap", {})
        self.assertEqual(status, 200)
        self.assertIsInstance(first, dict)
        self.assertEqual(first["type"], "cloudagent.admin.showcase_bootstrap")
        self.assertEqual(set(first["created"]), {"agent", "environment", "session", "job", "worker", "run"})
        self.assertEqual(first["run"]["status"], "queued")
        self.assertNotIn("lease_token", json.dumps(first))
        self.assertEqual(first["session"]["agent_snapshot"]["id"], first["agent"]["id"])
        self.assertEqual(first["session"]["environment_snapshot"]["id"], first["environment"]["id"])
        self.assertEqual(first["job"]["agent_id"], first["agent"]["id"])
        self.assertEqual(first["job"]["environment_id"], first["environment"]["id"])
        self.assertEqual(first["run"]["job_id"], first["job"]["id"])
        self.assertEqual(first["run"]["session_id"], first["session"]["id"])
        self.assertEqual(first["worker"]["id"], "showcase-worker")

        status, second, _ = self.request("POST", "/api/v1/admin/showcase/bootstrap", {})
        self.assertEqual(status, 200)
        self.assertIsInstance(second, dict)
        self.assertEqual(second["created"], [])
        self.assertEqual(set(second["reused"]), {"agent", "environment", "session", "job", "worker", "run"})
        for resource in ("agent", "environment", "session", "job", "worker", "run"):
            self.assertEqual(second[resource]["id"], first[resource]["id"])

        status, overview, _ = self.request("GET", "/api/v1/admin/overview")
        self.assertEqual(status, 200)
        self.assertIsInstance(overview, dict)
        self.assertIn("counts", overview)
        self.assertIn("recent_runs", overview)
        self.assertEqual(overview["counts"]["agents"], 1)
        self.assertEqual(overview["counts"]["sessions"], 1)
        self.assertEqual(overview["counts"]["jobs"], 1)
        self.assertEqual(overview["counts"]["workers"], 1)
        self.assertEqual(overview["signals"]["queue_depth"], 1)
        self.assertEqual(overview["signals"]["active_workers"], 1)
        self.assertEqual(overview["signals"]["pending_approvals"], 0)
        self.assertEqual(overview["signals"]["completed_runs"], 0)
        self.assertEqual(overview["readiness"]["status"], "local_showcase")
        self.assertEqual(overview["capability_boundary"]["local_showcase"], True)
        self.assertEqual(overview["capability_boundary"]["production_sandbox"], False)
        self.assertEqual(overview["capability_boundary"]["vault_injection"], False)
        self.assertEqual(
            {stage["id"] for stage in overview["runtime_rail"]},
            {"api_gateway", "policy", "queue", "worker", "artifact_audit"},
        )
        self.assertTrue(overview["activity"]["items"])
        self.assertNotIn("lease_token", json.dumps(overview))

        status, artifacts, _ = self.request("GET", "/api/v1/artifacts")
        self.assertEqual(status, 200)
        self.assertIsInstance(artifacts, dict)
        self.assertEqual(artifacts["data"], [])

        status, created_policy, _ = self.request(
            "POST",
            "/api/v1/tool-policies",
            {"scope": "artifact.create", "mode": "always_ask"},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(created_policy, dict)

        status, policies, _ = self.request("GET", "/api/v1/tool-policies")
        self.assertEqual(status, 200)
        self.assertIsInstance(policies, dict)
        self.assertEqual([item["id"] for item in policies["data"]], [created_policy["id"]])

    def test_bootstrap_requires_existing_admin_authentication(self) -> None:
        status, payload, _ = self.request("POST", "/api/v1/admin/showcase/bootstrap", {}, token=None)
        self.assertEqual(status, 401)
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["error"]["type"], "authentication_error")


if __name__ == "__main__":
    unittest.main()
