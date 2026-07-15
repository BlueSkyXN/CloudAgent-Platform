from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cloudagent_platform.app import Runtime, Store, make_handler
from cloudagent_platform.runtime import CodexCliProbeAdapter
from cloudagent_platform.worker import WorkerConfig, run_once


class LocalPrototypeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        db_path = str(Path(self.tmp.name) / "test.sqlite3")
        self.runtime = Runtime(Store(db_path), "test-token")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.runtime))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

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
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, dict | str]:
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if extra_headers:
            headers.update(extra_headers)
        req = Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with urlopen(req, timeout=5) as response:
                return response.status, self.decode(response)
        except HTTPError as exc:
            return exc.code, self.decode(exc)

    def decode(self, response: HTTPResponse | HTTPError) -> dict | str:
        raw = response.read().decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def start_fake_connector(self) -> tuple[ThreadingHTTPServer, threading.Thread, list[dict]]:
        records: list[dict] = []

        class ConnectorHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length).decode("utf-8")
                body = json.loads(raw) if raw else {}
                parsed = urlparse(self.path)
                records.append(
                    {
                        "path": parsed.path,
                        "query": parse_qs(parsed.query),
                        "headers": dict(self.headers),
                        "body": body,
                    }
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"ok": True, "path": parsed.path}).encode("utf-8")
                )

            def log_message(self, format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), ConnectorHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, records

    def test_health_and_auth_negative(self) -> None:
        status, payload = self.request("GET", "/_ops/healthz", token=None)
        self.assertEqual(status, 200)
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["status"], "ok")

        status, payload = self.request("GET", "/api/v1/agents", token=None)
        self.assertEqual(status, 401)
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["error"]["type"], "authentication_error")

        status, kernels = self.request("GET", "/api/v1/kernels")
        self.assertEqual(status, 200)
        self.assertIsInstance(kernels, dict)
        self.assertEqual(kernels["data"][0]["adapter_id"], "local-prototype-adapter")
        self.assertEqual(kernels["data"][0]["capabilities"]["artifact"], True)
        self.assertIn("codex-cli-probe", [kernel["id"] for kernel in kernels["data"]])

    def test_openapi_system_info_and_kernel_probe_contract(self) -> None:
        status, payload = self.request("GET", "/api/v1/system/info", token=None)
        self.assertEqual(status, 401)
        self.assertIsInstance(payload, dict)

        status, info = self.request("GET", "/api/v1/system/info")
        self.assertEqual(status, 200)
        self.assertIsInstance(info, dict)
        self.assertIn("permission_profiles", info)
        self.assertIn("sandbox_profiles", info)

        status, openapi = self.request("GET", "/openapi.json", token=None)
        self.assertEqual(status, 200)
        self.assertIsInstance(openapi, dict)
        self.assertEqual(openapi["openapi"], "3.1.0")
        self.assertIn("/api/v1/kernels/{kernel_id}/probe", openapi["paths"])

        status, kernel = self.request("GET", "/api/v1/kernels/codex-cli-probe")
        self.assertEqual(status, 200)
        self.assertIsInstance(kernel, dict)
        self.assertEqual(kernel["id"], "codex-cli-probe")
        self.assertEqual(kernel["capabilities"]["probe"], True)

        status, probe = self.request("POST", "/api/v1/kernels/codex-cli-probe/probe", {})
        self.assertEqual(status, 200)
        self.assertIsInstance(probe, dict)
        self.assertEqual(probe["dry_run"], True)
        self.assertIn("available", probe["probe"])

    def test_permission_and_sandbox_profile_contracts(self) -> None:
        status, profiles = self.request("GET", "/api/v1/permission-profiles")
        self.assertEqual(status, 200)
        self.assertIsInstance(profiles, dict)
        profile_ids = [item["id"] for item in profiles["data"]]
        self.assertIn("read-only", profile_ids)
        self.assertIn("workspace-write", profile_ids)
        self.assertIn("danger-full-access", profile_ids)
        danger = next(item for item in profiles["data"] if item["id"] == "danger-full-access")
        self.assertEqual(danger["status"], "blocked")
        self.assertEqual(danger["create_environment_allowed"], False)

        status, sandbox_profiles = self.request("GET", "/api/v1/sandbox-profiles")
        self.assertEqual(status, 200)
        self.assertIsInstance(sandbox_profiles, dict)
        sandbox_ids = [item["id"] for item in sandbox_profiles["data"]]
        self.assertIn("local-subprocess-deny-all", sandbox_ids)
        self.assertIn("seccomp-chroot-network-gated", sandbox_ids)

        status, error = self.request(
            "POST",
            "/api/v1/environments",
            {"name": "Unsafe env", "permission_profile_id": "danger-full-access"},
        )
        self.assertEqual(status, 400)
        self.assertIsInstance(error, dict)

        status, error = self.request(
            "POST",
            "/api/v1/environments",
            {"name": "Planned sandbox env", "sandbox_profile_id": "docker-deny-all"},
        )
        self.assertEqual(status, 400)
        self.assertIsInstance(error, dict)

        status, error = self.request(
            "POST",
            "/api/v1/environments",
            {
                "name": "Readonly weakened env",
                "permission_profile_id": "read-only",
                "tool_policy_defaults": {"external.http": "always_allow"},
            },
        )
        self.assertEqual(status, 400)
        self.assertIsInstance(error, dict)

        status, strict_env = self.request(
            "POST",
            "/api/v1/environments",
            {
                "name": "Readonly strict env",
                "permission_profile_id": "read-only",
                "tool_policy_defaults": {"artifact.create": "always_deny"},
            },
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(strict_env, dict)
        self.assertEqual(strict_env["tool_policy_defaults"]["artifact.create"], "always_deny")

        status, error = self.request(
            "POST",
            "/api/v1/environments",
            {
                "name": "Readonly writable env",
                "permission_profile_id": "read-only",
                "filesystem_policy": {
                    "workspace_root": "/workspace",
                    "writable_paths": ["/workspace"],
                    "read_only_root": True,
                    "allow_host_mounts": False,
                    "allow_docker_socket": False,
                },
            },
        )
        self.assertEqual(status, 400)
        self.assertIsInstance(error, dict)

        status, error = self.request(
            "POST",
            "/api/v1/environments",
            {
                "name": "Network escalation env",
                "permission_profile_id": "workspace-write",
                "network_policy": {"mode": "allow_all"},
            },
        )
        self.assertEqual(status, 400)
        self.assertIsInstance(error, dict)

        status, env = self.request(
            "POST",
            "/api/v1/environments",
            {
                "name": "Readonly env",
                "runtime_type": "local-subprocess",
                "permission_profile_id": "read-only",
                "sandbox_profile_id": "local-subprocess-deny-all",
            },
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(env, dict)
        self.assertEqual(env["permission_profile_id"], "read-only")
        self.assertEqual(env["sandbox_profile_id"], "local-subprocess-deny-all")
        self.assertEqual(env["filesystem_policy"]["writable_paths"], [])
        self.assertEqual(env["tool_policy_defaults"]["artifact.create"], "always_ask")
        self.assertEqual(env["tool_policy_defaults"]["external.http"], "always_deny")

        status, agent = self.request("POST", "/api/v1/agents", {"name": "Policy agent"})
        self.assertEqual(status, 201)
        self.assertIsInstance(agent, dict)
        status, session = self.request(
            "POST",
            "/api/v1/sessions",
            {"agent_id": agent["id"], "environment_id": env["id"]},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(session, dict)

        status, requested = self.request(
            "POST",
            f"/api/v1/sessions/{session['id']}/events",
            {
                "type": "tool.requested",
                "payload": {
                    "tool": "artifact.create",
                    "args": {"name": "needs-approval.txt", "content": "gated"},
                },
            },
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(requested, dict)
        self.assertEqual(requested["payload"]["policy_mode"], "always_ask")
        self.assertEqual(requested["payload"]["evaluated_permission"], "ask")

        status, pending = self.request("GET", f"/api/v1/sessions/{session['id']}/pending-actions")
        self.assertEqual(status, 200)
        self.assertIsInstance(pending, dict)
        self.assertEqual(pending["data"][0]["tool"], "artifact.create")
        self.assertEqual(pending["data"][0]["status"], "pending")

        status, policy_error = self.request(
            "POST",
            "/api/v1/tool-policies",
            {"scope": "external.http", "mode": "always_allow"},
        )
        self.assertEqual(status, 400)
        self.assertIsInstance(policy_error, dict)
        status, unavailable = self.request(
            "POST",
            f"/api/v1/sessions/{session['id']}/events",
            {
                "type": "tool.requested",
                "payload": {
                    "tool": "external.http",
                    "args": {"url": "https://example.invalid", "method": "GET"},
                },
            },
        )
        self.assertEqual(status, 400)
        self.assertIsInstance(unavailable, dict)

        status, tools = self.request("GET", "/api/v1/tools")
        self.assertEqual(status, 200)
        self.assertIsInstance(tools, dict)
        external_http = next(item for item in tools["data"] if item["name"] == "external.http")
        self.assertEqual(external_http["status"], "reference_only")
        self.assertFalse(external_http["executable"])
        self.assertEqual(external_http["effective_policy"]["decision"], "deny")
        self.assertEqual(external_http["effective_policy"]["source"], "capability_boundary")
        self.assertEqual(external_http["risk"], "high")

    def test_admin_does_not_embed_default_token_and_body_limit(self) -> None:
        status, html = self.request("GET", "/admin", token=None)
        self.assertEqual(status, 200)
        self.assertIsInstance(html, str)
        self.assertNotIn('value="dev-local-token"', html)

        self.runtime.max_json_bytes = 20
        status, payload = self.request("POST", "/api/v1/agents", {"name": "x" * 32})
        self.assertEqual(status, 413)
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["error"]["type"], "payload_too_large_error")

    def test_codex_probe_adapter_is_safe_and_deterministic_with_fake_runner(self) -> None:
        adapter = CodexCliProbeAdapter(
            which=lambda _: "/usr/local/bin/codex",
            runner=lambda command, timeout: subprocess.CompletedProcess(
                command,
                0,
                stdout="codex-cli 0.test\n",
                stderr="",
            ),
        )
        manifest = adapter.manifest()
        self.assertEqual(manifest["kernel_id"], "codex-cli-probe")
        self.assertEqual(manifest["probe"]["available"], True)
        self.assertEqual(manifest["probe"]["version"], "codex-cli 0.test")
        self.assertEqual(manifest["capabilities"]["file_edit"], False)

    def test_session_event_and_sse_once(self) -> None:
        status, agent = self.request(
            "POST",
            "/api/v1/agents",
            {"name": "Test agent", "kernel": {"id": "codex-cli-local"}},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(agent, dict)

        status, session = self.request(
            "POST",
            "/api/v1/sessions",
            {"agent": {"id": agent["id"]}},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(session, dict)

        status, queued = self.request(
            "POST",
            f"/api/v1/sessions/{session['id']}/events",
            {"type": "user.message", "payload": {"text": "run"}},
        )
        self.assertEqual(status, 202)
        self.assertIsInstance(queued, dict)
        self.assertEqual(queued["type"], "session_turn_queued")
        self.assertEqual(queued["event"]["type"], "user.message")
        self.assertEqual(queued["run"]["status"], "queued")
        self.assertEqual(queued["session"]["status"], "queued")

        status, events = self.request("GET", f"/api/v1/sessions/{session['id']}/events")
        self.assertEqual(status, 200)
        self.assertIsInstance(events, dict)
        self.assertGreaterEqual(len(events["data"]), 4)
        pre_worker_event_types = [event["type"] for event in events["data"]]
        self.assertIn("run.queued", pre_worker_event_types)
        self.assertIn("session.turn_queued", pre_worker_event_types)
        self.assertNotIn("kernel.started", pre_worker_event_types)
        self.assertNotIn("session.idle", pre_worker_event_types)

        result = run_once(
            WorkerConfig(
                base_url=self.base,
                token="test-token",
                worker_id="worker_event_1",
                name="Session event worker",
                lease_seconds=60,
            )
        )
        self.assertEqual(result["claimed"], True)
        self.assertEqual(result["run"]["id"], queued["run"]["id"])
        self.assertEqual(result["run"]["status"], "succeeded")

        req = Request(
            self.base + f"/api/v1/sessions/{session['id']}/events/stream?once=1",
            headers={"Authorization": "Bearer test-token"},
            method="GET",
        )
        with urlopen(req, timeout=5) as response:
            stream = response.read().decode("utf-8")
        self.assertIn("event: user.message", stream)
        self.assertIn("event: session.idle", stream)

    def test_integration_secret_is_redacted_and_job_trigger_creates_session(self) -> None:
        status, agent = self.request(
            "POST",
            "/api/v1/agents",
            {"name": "Scheduler agent", "kernel": {"id": "codex-cli-local"}},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(agent, dict)

        status, integration = self.request(
            "POST",
            "/api/v1/integrations",
            {
                "provider": "dify",
                "name": "Dify v2",
                "base_url": "https://dify.example/v1",
                "token": "secret-token",
            },
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(integration, dict)
        self.assertEqual(integration["provider"], "dify")
        self.assertNotIn("secret-token", json.dumps(integration))
        self.assertTrue(integration["secret_ref"].startswith("sha256:"))

        status, job = self.request(
            "POST",
            "/api/v1/jobs",
            {"name": "Manual run", "agent_id": agent["id"], "trigger": {"type": "manual"}},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(job, dict)

        status, result = self.request("POST", f"/api/v1/jobs/{job['id']}/trigger", {})
        self.assertEqual(status, 202)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["session"]["status"], "queued")
        self.assertEqual(result["job"]["last_run_at"] is not None, True)
        self.assertEqual(result["run"]["status"], "queued")
        self.assertIsNone(result["run"]["worker_id"])

        status, events = self.request("GET", f"/api/v1/sessions/{result['session']['id']}/events")
        self.assertEqual(status, 200)
        self.assertIsInstance(events, dict)
        event_types = [event["type"] for event in events["data"]]
        self.assertIn("run.queued", event_types)
        self.assertIn("job.triggered", event_types)
        self.assertNotIn("kernel.started", event_types)

    def test_vault_credentials_are_write_only_and_sessions_bind_vault_refs(self) -> None:
        status, vault = self.request(
            "POST",
            "/api/v1/vaults",
            {"display_name": "GitHub vault", "metadata": {"scope": "unit-test"}},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(vault, dict)
        self.assertEqual(vault["type"], "vault")
        self.assertEqual(vault["credentials"], [])

        status, missing_vault = self.request("GET", "/api/v1/vaults/vault_missing/credentials")
        self.assertEqual(status, 404)
        self.assertIsInstance(missing_vault, dict)

        status, missing_secret = self.request(
            "POST",
            f"/api/v1/vaults/{vault['id']}/credentials",
            {"auth": {"type": "static_bearer", "mcp_server_url": "https://github.example/mcp"}},
        )
        self.assertEqual(status, 400)
        self.assertIsInstance(missing_secret, dict)

        status, credential = self.request(
            "POST",
            f"/api/v1/vaults/{vault['id']}/credentials",
            {
                "auth": {
                    "type": "static_bearer",
                    "mcp_server_url": "https://github.example/mcp",
                    "token": "vault-secret-token",
                }
            },
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(credential, dict)
        self.assertEqual(credential["type"], "vault_credential")
        self.assertEqual(credential["status"], "reference_only")
        self.assertNotIn("vault-secret-token", json.dumps(credential))
        self.assertTrue(credential["auth"]["secret_ref"].startswith("sha256:"))
        stored = self.runtime.store.fetch_one(
            "SELECT * FROM vault_credentials WHERE id = ?",
            (credential["id"],),
        )
        self.assertIsNotNone(stored)
        self.assertNotIn("secret_material", stored.keys())
        self.assertNotIn("vault-secret-token", json.dumps(dict(stored)))

        status, vault_after = self.request("GET", f"/api/v1/vaults/{vault['id']}")
        self.assertEqual(status, 200)
        self.assertIsInstance(vault_after, dict)
        self.assertEqual(len(vault_after["credentials"]), 1)
        self.assertNotIn("vault-secret-token", json.dumps(vault_after))

        status, agent = self.request("POST", "/api/v1/agents", {"name": "Vault agent"})
        self.assertEqual(status, 201)
        self.assertIsInstance(agent, dict)
        status, session = self.request(
            "POST",
            "/api/v1/sessions",
            {"agent_id": agent["id"], "vault_ids": [vault["id"]]},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(session, dict)
        self.assertEqual(session["vault_ids"], [vault["id"]])

        status, queued = self.request(
            "POST",
            f"/api/v1/sessions/{session['id']}/events",
            {"type": "user.message", "payload": {"text": "run with vault"}},
        )
        self.assertEqual(status, 202)
        self.assertIsInstance(queued, dict)
        result = run_once(
            WorkerConfig(
                base_url=self.base,
                token="test-token",
                worker_id="worker_vault_1",
                name="Vault worker",
                lease_seconds=60,
            )
        )
        self.assertEqual(result["claimed"], True)
        self.assertEqual(result["run"]["status"], "succeeded")

        status, events = self.request("GET", f"/api/v1/sessions/{session['id']}/events")
        self.assertEqual(status, 200)
        self.assertIsInstance(events, dict)
        policy_events = [event for event in events["data"] if event["type"] == "runtime.policy_applied"]
        self.assertEqual(policy_events[0]["payload"]["policy"]["vault_ids"], [vault["id"]])
        self.assertEqual(policy_events[0]["payload"]["policy"]["vaults_bound_count"], 1)
        self.assertNotIn("vault-secret-token", json.dumps(events))

    def test_store_migration_scrubs_legacy_vault_secret_and_backfills_policy(self) -> None:
        database = str(Path(self.tmp.name) / "legacy.sqlite3")
        store = Store(database)
        try:
            vault = store.create_vault({"display_name": "Legacy vault"}, "legacy")
            credential = store.create_vault_credential(
                vault["id"],
                {"auth": {"type": "static_bearer", "token": "discarded-secret"}},
                "legacy",
            )
            store.conn.execute("ALTER TABLE vault_credentials ADD COLUMN secret_material TEXT")
            store.conn.execute(
                "UPDATE vault_credentials SET secret_material = ? WHERE id = ?",
                ("legacy-plaintext", credential["id"]),
            )
            store.conn.execute("UPDATE environments SET tool_policy_defaults = '{}'")
            store.conn.commit()
        finally:
            store.close()

        migrated = Store(database)
        try:
            row = migrated.fetch_one(
                "SELECT secret_material FROM vault_credentials WHERE id = ?",
                (credential["id"],),
            )
            self.assertIsNotNone(row)
            self.assertIsNone(row["secret_material"])
            environment = migrated.list_environments()[0]
            self.assertEqual(
                environment["tool_policy_defaults"]["external.http"],
                "always_ask",
            )
        finally:
            migrated.close()

    def test_worker_registration_heartbeat_and_job_run_assignment(self) -> None:
        status, worker = self.request(
            "POST",
            "/api/v1/workers",
            {
                "id": "worker_local_1",
                "name": "Local worker",
                "capabilities": {"local_noop_turn": True, "session_events": True},
            },
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(worker, dict)
        self.assertEqual(worker["id"], "worker_local_1")

        status, worker = self.request("POST", "/api/v1/workers/worker_local_1/heartbeat", {})
        self.assertEqual(status, 200)
        self.assertIsInstance(worker, dict)
        self.assertEqual(worker["status"], "active")

        status, agent = self.request(
            "POST",
            "/api/v1/agents",
            {"name": "Runtime agent", "kernel": {"id": "codex-cli-local"}},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(agent, dict)
        status, job = self.request(
            "POST",
            "/api/v1/jobs",
            {"name": "Worker-backed run", "agent_id": agent["id"], "trigger": {"type": "manual"}},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(job, dict)

        status, accepted = self.request("POST", f"/api/v1/jobs/{job['id']}/trigger", {})
        self.assertEqual(status, 202)
        self.assertIsInstance(accepted, dict)
        self.assertIsNone(accepted["run"]["worker_id"])
        self.assertEqual(accepted["run"]["status"], "queued")
        self.assertEqual(accepted["session"]["status"], "queued")

        status, events = self.request("GET", f"/api/v1/sessions/{accepted['session']['id']}/events")
        self.assertEqual(status, 200)
        self.assertIsInstance(events, dict)
        self.assertNotIn("kernel.started", [event["type"] for event in events["data"]])

        result = run_once(
            WorkerConfig(
                base_url=self.base,
                token="test-token",
                worker_id="worker_local_1",
                name="Local worker",
                lease_seconds=60,
            )
        )
        self.assertEqual(result["claimed"], True)
        self.assertEqual(result["run"]["id"], accepted["run"]["id"])
        self.assertEqual(result["run"]["worker_id"], "worker_local_1")
        self.assertEqual(result["run"]["status"], "succeeded")

        status, runs = self.request("GET", "/api/v1/runs")
        self.assertEqual(status, 200)
        self.assertIsInstance(runs, dict)
        self.assertEqual(runs["data"][0]["id"], result["run"]["id"])

        status, usage = self.request("GET", f"/api/v1/sessions/{accepted['session']['id']}/usage")
        self.assertEqual(status, 200)
        self.assertIsInstance(usage, dict)
        self.assertEqual(usage["data"][0]["run_id"], result["run"]["id"])
        self.assertEqual(usage["data"][0]["worker_id"], "worker_local_1")

        status, events = self.request("GET", f"/api/v1/sessions/{accepted['session']['id']}/events")
        self.assertEqual(status, 200)
        self.assertIsInstance(events, dict)
        event_types = [event["type"] for event in events["data"]]
        self.assertIn("job.triggered", event_types)
        self.assertIn("worker.claimed", event_types)
        self.assertIn("worker.assigned", event_types)
        self.assertIn("kernel.started", event_types)
        self.assertIn("sandbox.created", event_types)
        self.assertIn("sandbox.stdout", event_types)
        self.assertIn("sandbox.cleanup", event_types)
        self.assertIn("kernel.completed", event_types)
        cleanup_events = [event for event in events["data"] if event["type"] == "sandbox.cleanup"]
        self.assertEqual(cleanup_events[0]["payload"]["workspace_deleted"], True)

        status, audit = self.request("GET", f"/api/v1/sessions/{accepted['session']['id']}/audit")
        self.assertEqual(status, 200)
        self.assertIsInstance(audit, dict)
        self.assertIn("event.append", [item["action"] for item in audit["data"]])

        status, worker_after = self.request("GET", "/api/v1/workers")
        self.assertEqual(status, 200)
        self.assertIsInstance(worker_after, dict)
        self.assertIsNone(worker_after["data"][0]["active_run_id"])

    def test_job_enqueue_worker_claim_and_legacy_execute_still_works(self) -> None:
        status, worker = self.request(
            "POST",
            "/api/v1/workers",
            {"id": "worker_async_1", "name": "Async worker"},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(worker, dict)

        status, agent = self.request(
            "POST",
            "/api/v1/agents",
            {"name": "Async runtime agent", "kernel": {"id": "codex-cli-local"}},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(agent, dict)
        status, job = self.request(
            "POST",
            "/api/v1/jobs",
            {"name": "Queued run", "agent_id": agent["id"], "trigger": {"type": "manual"}},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(job, dict)

        status, enqueued = self.request("POST", f"/api/v1/jobs/{job['id']}/enqueue", {})
        self.assertEqual(status, 202)
        self.assertIsInstance(enqueued, dict)
        self.assertEqual(enqueued["run"]["status"], "queued")
        self.assertIsNone(enqueued["run"]["worker_id"])
        self.assertEqual(enqueued["session"]["status"], "queued")

        status, claim = self.request(
            "POST",
            "/api/v1/workers/worker_async_1/claim",
            {"lease_seconds": 60},
        )
        self.assertEqual(status, 200)
        self.assertIsInstance(claim, dict)
        self.assertEqual(claim["run"]["id"], enqueued["run"]["id"])
        self.assertEqual(claim["run"]["status"], "running")
        self.assertEqual(claim["run"]["worker_id"], "worker_async_1")
        self.assertTrue(claim["run"]["lease_token"].startswith("lease_"))
        self.assertEqual(claim["run"]["lease_generation"], 1)
        self.assertEqual(claim["worker"]["active_run_id"], enqueued["run"]["id"])

        status, public_runs = self.request("GET", "/api/v1/runs")
        self.assertEqual(status, 200)
        self.assertIsInstance(public_runs, dict)
        self.assertNotIn("lease_token", public_runs["data"][0])

        status, overview = self.request("GET", "/api/v1/admin/overview")
        self.assertEqual(status, 200)
        self.assertIsInstance(overview, dict)
        self.assertNotIn("lease_token", overview["recent_runs"][0])
        self.assertNotIn(claim["run"]["lease_token"], json.dumps(overview))

        status, execute = self.request(
            "POST",
            f"/api/v1/workers/worker_async_1/runs/{enqueued['run']['id']}/execute",
            {"lease_token": claim["run"]["lease_token"]},
        )
        self.assertEqual(status, 200)
        self.assertIsInstance(execute, dict)
        self.assertEqual(execute["run"]["status"], "succeeded")

        status, usage = self.request("GET", f"/api/v1/sessions/{enqueued['session']['id']}/usage")
        self.assertEqual(status, 200)
        self.assertIsInstance(usage, dict)
        self.assertEqual(usage["data"][0]["run_id"], enqueued["run"]["id"])
        self.assertEqual(usage["data"][0]["worker_id"], "worker_async_1")

        status, events = self.request("GET", f"/api/v1/sessions/{enqueued['session']['id']}/events")
        self.assertEqual(status, 200)
        self.assertIsInstance(events, dict)
        event_types = [event["type"] for event in events["data"]]
        self.assertIn("run.queued", event_types)
        self.assertIn("job.enqueued", event_types)
        self.assertIn("worker.claimed", event_types)
        self.assertIn("run.completed", event_types)

        status, worker_after = self.request("GET", "/api/v1/workers")
        self.assertEqual(status, 200)
        self.assertIsInstance(worker_after, dict)
        self.assertIsNone(worker_after["data"][0]["active_run_id"])

    def test_worker_cli_run_once_consumes_queued_run_over_http(self) -> None:
        status, agent = self.request(
            "POST",
            "/api/v1/agents",
            {"name": "Worker CLI agent", "kernel": {"id": "codex-cli-local"}},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(agent, dict)
        status, job = self.request(
            "POST",
            "/api/v1/jobs",
            {"name": "Worker CLI job", "agent_id": agent["id"], "trigger": {"type": "manual"}},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(job, dict)
        status, enqueued = self.request("POST", f"/api/v1/jobs/{job['id']}/enqueue", {})
        self.assertEqual(status, 202)
        self.assertIsInstance(enqueued, dict)

        result = run_once(
            WorkerConfig(
                base_url=self.base,
                token="test-token",
                worker_id="worker_cli_1",
                name="CLI worker",
                lease_seconds=60,
            )
        )
        self.assertEqual(result["claimed"], True)
        self.assertEqual(result["run"]["id"], enqueued["run"]["id"])
        self.assertEqual(result["run"]["status"], "succeeded")
        self.assertEqual(result["run"]["result"]["completed_by"], "worker_cli_1")
        self.assertEqual(result["run"]["result"]["execution_mode"], "worker-local")
        self.assertEqual(result["execution"]["type"], "worker_run_completion")

        status, workers = self.request("GET", "/api/v1/workers")
        self.assertEqual(status, 200)
        self.assertIsInstance(workers, dict)
        cli_workers = [item for item in workers["data"] if item["id"] == "worker_cli_1"]
        self.assertEqual(cli_workers[0]["active_run_id"], None)

        status, events = self.request("GET", f"/api/v1/sessions/{enqueued['session']['id']}/events")
        self.assertEqual(status, 200)
        self.assertIsInstance(events, dict)
        event_types = [event["type"] for event in events["data"]]
        self.assertIn("session.running", event_types)
        self.assertIn("runtime.policy_applied", event_types)
        self.assertIn("kernel.started", event_types)
        self.assertIn("sandbox.created", event_types)
        self.assertIn("sandbox.cleanup", event_types)
        self.assertIn("session.idle", event_types)
        policy_events = [event for event in events["data"] if event["type"] == "runtime.policy_applied"]
        self.assertEqual(policy_events[0]["payload"]["permission_profile_id"], "workspace-write")
        self.assertEqual(policy_events[0]["payload"]["sandbox_profile_id"], "local-subprocess-deny-all")
        sandbox_events = [event for event in events["data"] if event["type"] == "sandbox.created"]
        self.assertEqual(sandbox_events[0]["payload"]["permission_profile_id"], "workspace-write")
        self.assertEqual(sandbox_events[0]["payload"]["sandbox_profile_id"], "local-subprocess-deny-all")

    def test_worker_writeback_requires_assigned_worker(self) -> None:
        status, agent = self.request(
            "POST",
            "/api/v1/agents",
            {"name": "Worker writeback guard agent", "kernel": {"id": "codex-cli-local"}},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(agent, dict)
        status, job = self.request(
            "POST",
            "/api/v1/jobs",
            {"name": "Guarded queued run", "agent_id": agent["id"], "trigger": {"type": "manual"}},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(job, dict)
        status, enqueued = self.request("POST", f"/api/v1/jobs/{job['id']}/enqueue", {})
        self.assertEqual(status, 202)
        self.assertIsInstance(enqueued, dict)

        status, _ = self.request("POST", "/api/v1/workers", {"id": "worker_owner", "name": "Owner"})
        self.assertEqual(status, 201)
        status, _ = self.request("POST", "/api/v1/workers", {"id": "worker_intruder", "name": "Intruder"})
        self.assertEqual(status, 201)

        status, claim = self.request(
            "POST",
            "/api/v1/workers/worker_owner/claim",
            {"lease_seconds": 60},
        )
        self.assertEqual(status, 200)
        self.assertIsInstance(claim, dict)
        self.assertEqual(claim["run"]["id"], enqueued["run"]["id"])
        lease_token = claim["run"]["lease_token"]

        status, error = self.request(
            "POST",
            f"/api/v1/workers/worker_intruder/runs/{enqueued['run']['id']}/turn/start",
            {},
        )
        self.assertEqual(status, 401)
        self.assertIsInstance(error, dict)
        self.assertEqual(error["error"]["type"], "authentication_error")

        status, error = self.request(
            "POST",
            f"/api/v1/workers/worker_owner/runs/{enqueued['run']['id']}/turn/start",
            {},
        )
        self.assertEqual(status, 401)
        self.assertIsInstance(error, dict)
        self.assertEqual(error["error"]["type"], "authentication_error")

        status, started = self.request(
            "POST",
            f"/api/v1/workers/worker_owner/runs/{enqueued['run']['id']}/turn/start",
            {"lease_token": lease_token},
        )
        self.assertEqual(status, 200)
        self.assertIsInstance(started, dict)
        self.assertTrue(started["turn_id"].startswith("turn_"))

    def test_stale_worker_lease_cannot_complete_reclaimed_run(self) -> None:
        status, agent = self.request(
            "POST",
            "/api/v1/agents",
            {"name": "Lease fenced agent", "kernel": {"id": "codex-cli-local"}},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(agent, dict)
        status, job = self.request(
            "POST",
            "/api/v1/jobs",
            {"name": "Lease fenced queued run", "agent_id": agent["id"], "trigger": {"type": "manual"}},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(job, dict)
        status, enqueued = self.request("POST", f"/api/v1/jobs/{job['id']}/enqueue", {})
        self.assertEqual(status, 202)
        self.assertIsInstance(enqueued, dict)

        status, _ = self.request("POST", "/api/v1/workers", {"id": "worker_stale", "name": "Stale worker"})
        self.assertEqual(status, 201)
        status, first_claim = self.request(
            "POST",
            "/api/v1/workers/worker_stale/claim",
            {"lease_seconds": 60},
        )
        self.assertEqual(status, 200)
        self.assertIsInstance(first_claim, dict)
        run_id = first_claim["run"]["id"]
        old_token = first_claim["run"]["lease_token"]
        self.assertEqual(run_id, enqueued["run"]["id"])

        expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with self.runtime.store._lock:
            self.runtime.store.conn.execute(
                "UPDATE job_runs SET lease_expires_at = ? WHERE id = ?",
                (expired_at, run_id),
            )
            self.runtime.store.conn.commit()
        self.assertEqual(self.runtime.store.requeue_expired_runs("test-requeue"), 1)

        status, second_claim = self.request(
            "POST",
            "/api/v1/workers/worker_stale/claim",
            {"lease_seconds": 60},
        )
        self.assertEqual(status, 200)
        self.assertIsInstance(second_claim, dict)
        self.assertEqual(second_claim["run"]["id"], run_id)
        self.assertNotEqual(second_claim["run"]["lease_token"], old_token)
        self.assertEqual(second_claim["run"]["lease_generation"], 2)

        status, stale_complete = self.request(
            "POST",
            f"/api/v1/workers/worker_stale/runs/{run_id}/complete",
            {"lease_token": old_token, "status": "succeeded", "result": {"stale": True}},
        )
        self.assertEqual(status, 401)
        self.assertIsInstance(stale_complete, dict)

        status, current_complete = self.request(
            "POST",
            f"/api/v1/workers/worker_stale/runs/{run_id}/complete",
            {
                "lease_token": second_claim["run"]["lease_token"],
                "status": "succeeded",
                "result": {"stale": False},
            },
        )
        self.assertEqual(status, 200)
        self.assertIsInstance(current_complete, dict)
        self.assertEqual(current_complete["run"]["status"], "succeeded")
        self.assertEqual(current_complete["run"]["result"], {"stale": False})

    def test_worker_can_renew_current_run_lease_only_before_expiry(self) -> None:
        status, agent = self.request(
            "POST",
            "/api/v1/agents",
            {"name": "Lease renew agent", "kernel": {"id": "codex-cli-local"}},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(agent, dict)
        status, job = self.request(
            "POST",
            "/api/v1/jobs",
            {"name": "Lease renew queued run", "agent_id": agent["id"], "trigger": {"type": "manual"}},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(job, dict)
        status, enqueued = self.request("POST", f"/api/v1/jobs/{job['id']}/enqueue", {})
        self.assertEqual(status, 202)
        self.assertIsInstance(enqueued, dict)

        status, _ = self.request("POST", "/api/v1/workers", {"id": "worker_renew", "name": "Renew worker"})
        self.assertEqual(status, 201)
        status, claim = self.request(
            "POST",
            "/api/v1/workers/worker_renew/claim",
            {"lease_seconds": 10},
        )
        self.assertEqual(status, 200)
        self.assertIsInstance(claim, dict)
        run_id = claim["run"]["id"]
        lease_token = claim["run"]["lease_token"]
        first_expiry = datetime.fromisoformat(claim["run"]["lease_expires_at"])

        status, renewed = self.request(
            "POST",
            f"/api/v1/workers/worker_renew/runs/{run_id}/lease/renew",
            {"lease_token": lease_token, "lease_seconds": 120},
        )
        self.assertEqual(status, 200)
        self.assertIsInstance(renewed, dict)
        self.assertEqual(renewed["type"], "worker_lease_renewal")
        self.assertEqual(renewed["run"]["lease_token"], lease_token)
        self.assertEqual(renewed["run"]["lease_generation"], claim["run"]["lease_generation"])
        self.assertGreater(datetime.fromisoformat(renewed["run"]["lease_expires_at"]), first_expiry)

        status, wrong_token = self.request(
            "POST",
            f"/api/v1/workers/worker_renew/runs/{run_id}/lease/renew",
            {"lease_token": "lease_wrong", "lease_seconds": 120},
        )
        self.assertEqual(status, 401)
        self.assertIsInstance(wrong_token, dict)

        expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with self.runtime.store._lock:
            self.runtime.store.conn.execute(
                "UPDATE job_runs SET lease_expires_at = ? WHERE id = ?",
                (expired_at, run_id),
            )
            self.runtime.store.conn.commit()
        status, expired = self.request(
            "POST",
            f"/api/v1/workers/worker_renew/runs/{run_id}/lease/renew",
            {"lease_token": lease_token, "lease_seconds": 120},
        )
        self.assertEqual(status, 401)
        self.assertIsInstance(expired, dict)

    def test_signed_webhook_trigger_creates_run_without_bearer_auth(self) -> None:
        status, agent = self.request(
            "POST",
            "/api/v1/agents",
            {"name": "Webhook agent", "kernel": {"id": "codex-cli-local"}},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(agent, dict)
        status, integration = self.request(
            "POST",
            "/api/v1/integrations",
            {
                "provider": "feishu",
                "name": "Feishu webhook",
                "base_url": "https://open.feishu.example",
                "token": "webhook-secret",
            },
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(integration, dict)

        status, error = self.request(
            "POST",
            f"/api/v1/webhooks/feishu/{integration['id']}",
            {"event": "ping"},
            token=None,
        )
        self.assertEqual(status, 401)
        self.assertIsInstance(error, dict)

        status, result = self.request(
            "POST",
            f"/api/v1/webhooks/feishu/{integration['id']}",
            {"event": "ping"},
            token=None,
            extra_headers={"X-CloudAgent-Webhook-Token": "webhook-secret"},
        )
        self.assertEqual(status, 202)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["type"], "webhook_trigger")
        self.assertEqual(result["integration"]["provider"], "feishu")
        self.assertNotIn("secret_ref", result["integration"])
        self.assertEqual(result["run"]["trigger_source"], "integration:feishu")
        self.assertEqual(result["run"]["status"], "queued")
        self.assertEqual(result["session"]["status"], "queued")

        status, events = self.request("GET", f"/api/v1/sessions/{result['session']['id']}/events")
        self.assertEqual(status, 200)
        self.assertIsInstance(events, dict)
        event_types = [event["type"] for event in events["data"]]
        self.assertIn("integration.webhook.received", event_types)
        self.assertNotIn("kernel.started", event_types)

    def test_files_artifacts_and_usage_are_queryable_after_turn(self) -> None:
        status, file_meta = self.request(
            "POST",
            "/api/v1/files",
            {"name": "input.txt", "content": "hello cloud agent"},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(file_meta, dict)
        self.assertEqual(file_meta["name"], "input.txt")
        self.assertEqual(file_meta["size"], len("hello cloud agent"))

        req = Request(
            self.base + f"/api/v1/files/{file_meta['id']}/content",
            headers={"Authorization": "Bearer test-token"},
            method="GET",
        )
        with urlopen(req, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read().decode("utf-8"), "hello cloud agent")

        status, agent = self.request(
            "POST",
            "/api/v1/agents",
            {"name": "Artifact agent", "kernel": {"id": "codex-cli-local"}},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(agent, dict)
        status, session = self.request("POST", "/api/v1/sessions", {"agent": {"id": agent["id"]}})
        self.assertEqual(status, 201)
        self.assertIsInstance(session, dict)

        status, queued = self.request(
            "POST",
            f"/api/v1/sessions/{session['id']}/events",
            {"type": "user.message", "payload": {"text": "produce an artifact"}},
        )
        self.assertEqual(status, 202)
        self.assertIsInstance(queued, dict)
        self.assertEqual(queued["run"]["status"], "queued")

        status, artifacts = self.request("GET", f"/api/v1/sessions/{session['id']}/artifacts")
        self.assertEqual(status, 200)
        self.assertIsInstance(artifacts, dict)
        self.assertEqual(len(artifacts["data"]), 0)

        result = run_once(
            WorkerConfig(
                base_url=self.base,
                token="test-token",
                worker_id="worker_artifact_1",
                name="Artifact worker",
                lease_seconds=60,
            )
        )
        self.assertEqual(result["claimed"], True)
        self.assertEqual(result["run"]["id"], queued["run"]["id"])
        self.assertEqual(result["run"]["status"], "succeeded")

        status, artifacts = self.request("GET", f"/api/v1/sessions/{session['id']}/artifacts")
        self.assertEqual(status, 200)
        self.assertIsInstance(artifacts, dict)
        self.assertGreaterEqual(len(artifacts["data"]), 1)
        self.assertEqual(artifacts["data"][0]["name"], "local-turn-summary.json")

        status, usage = self.request("GET", f"/api/v1/sessions/{session['id']}/usage")
        self.assertEqual(status, 200)
        self.assertIsInstance(usage, dict)
        self.assertGreaterEqual(len(usage["data"]), 1)
        self.assertEqual(usage["data"][0]["session_id"], session["id"])
        self.assertGreaterEqual(usage["data"][0]["tool_duration_ms"], 0)

        status, events = self.request("GET", f"/api/v1/sessions/{session['id']}/events")
        self.assertEqual(status, 200)
        self.assertIsInstance(events, dict)
        self.assertTrue(all(event["audit_ref"] for event in events["data"]))
        event_types = [event["type"] for event in events["data"]]
        self.assertIn("kernel.started", event_types)
        self.assertIn("sandbox.created", event_types)
        self.assertIn("sandbox.stdout", event_types)
        stdout_events = [event for event in events["data"] if event["type"] == "sandbox.stdout"]
        self.assertIn('"status": "ok"', stdout_events[0]["payload"]["stdout"])
        self.assertIn("artifact.created", [event["type"] for event in events["data"]])
        self.assertIn("usage.turn_summary", [event["type"] for event in events["data"]])

    def test_tool_gateway_pending_action_and_artifact_tool(self) -> None:
        status, agent = self.request(
            "POST",
            "/api/v1/agents",
            {"name": "Tool agent", "kernel": {"id": "codex-cli-local"}},
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(agent, dict)
        status, session = self.request("POST", "/api/v1/sessions", {"agent": {"id": agent["id"]}})
        self.assertEqual(status, 201)
        self.assertIsInstance(session, dict)

        status, tools = self.request("GET", "/api/v1/tools")
        self.assertEqual(status, 200)
        self.assertIsInstance(tools, dict)
        external_http = next(tool for tool in tools["data"] if tool["name"] == "external.http")
        self.assertEqual(external_http["status"], "reference_only")
        self.assertFalse(external_http["executable"])

        status, requested = self.request(
            "POST",
            f"/api/v1/sessions/{session['id']}/events",
            {
                "type": "tool.requested",
                "payload": {
                    "tool": "external.http",
                    "args": {"url": "https://example.invalid", "method": "GET"},
                },
            },
        )
        self.assertEqual(status, 400)
        self.assertIsInstance(requested, dict)

        status, _ = self.request(
            "POST",
            "/api/v1/tool-policies",
            {"scope": "file.read", "mode": "always_ask"},
        )
        self.assertEqual(status, 201)
        status, requested = self.request(
            "POST",
            f"/api/v1/sessions/{session['id']}/events",
            {
                "type": "tool.requested",
                "payload": {"tool": "file.read", "args": {"file_id": "file_not_used"}},
            },
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(requested, dict)

        status, pending = self.request("GET", f"/api/v1/sessions/{session['id']}/pending-actions")
        self.assertEqual(status, 200)
        self.assertIsInstance(pending, dict)
        self.assertEqual(pending["data"][0]["tool"], "file.read")
        self.assertEqual(pending["data"][0]["status"], "pending")

        action_id = pending["data"][0]["id"]
        status, resolved = self.request(
            "POST",
            f"/api/v1/sessions/{session['id']}/pending-actions/{action_id}/resolve",
            {"decision": "reject", "reason": "network not allowlisted"},
        )
        self.assertEqual(status, 200)
        self.assertIsInstance(resolved, dict)
        self.assertEqual(resolved["status"], "rejected")

        status, artifact_tool = self.request(
            "POST",
            f"/api/v1/sessions/{session['id']}/events",
            {
                "type": "tool.requested",
                "payload": {
                    "tool": "artifact.create",
                    "args": {"name": "tool-output.txt", "content": "created by tool gateway"},
                },
            },
        )
        self.assertEqual(status, 201)
        self.assertIsInstance(artifact_tool, dict)

        status, artifacts = self.request("GET", f"/api/v1/sessions/{session['id']}/artifacts")
        self.assertEqual(status, 200)
        self.assertIsInstance(artifacts, dict)
        self.assertEqual(len(artifacts["data"]), 0)

        status, actions = self.request("GET", f"/api/v1/sessions/{session['id']}/pending-actions")
        self.assertEqual(status, 200)
        self.assertIsInstance(actions, dict)
        artifact_actions = [item for item in actions["data"] if item["tool"] == "artifact.create"]
        self.assertEqual(len(artifact_actions), 1)
        self.assertEqual(artifact_actions[0]["status"], "approved")
        action_id = artifact_actions[0]["id"]

        status, _ = self.request("POST", "/api/v1/workers", {"id": "worker_tool_1", "name": "Tool worker"})
        self.assertEqual(status, 201)
        status, claim = self.request(
            "POST",
            "/api/v1/workers/worker_tool_1/claim",
            {"lease_seconds": 60},
        )
        self.assertEqual(status, 200)
        self.assertIsInstance(claim, dict)
        self.assertEqual(claim["run"]["trigger_source"], "tool:artifact.create")
        self.assertEqual(claim["run"]["session_id"], session["id"])

        status, tool_execution = self.request(
            "POST",
            f"/api/v1/workers/worker_tool_1/runs/{claim['run']['id']}/tools/execute",
            {"lease_token": claim["run"]["lease_token"], "action_id": action_id},
        )
        self.assertEqual(status, 200)
        self.assertIsInstance(tool_execution, dict)
        self.assertEqual(tool_execution["type"], "worker_tool_execution")
        self.assertEqual(tool_execution["action"]["status"], "executed")

        status, artifacts = self.request("GET", f"/api/v1/sessions/{session['id']}/artifacts")
        self.assertEqual(status, 200)
        self.assertIsInstance(artifacts, dict)
        self.assertEqual(artifacts["data"][0]["name"], "tool-output.txt")

        status, complete = self.request(
            "POST",
            f"/api/v1/workers/worker_tool_1/runs/{claim['run']['id']}/complete",
            {
                "lease_token": claim["run"]["lease_token"],
                "status": "succeeded",
                "result": {"tool_action_id": action_id},
            },
        )
        self.assertEqual(status, 200)
        self.assertIsInstance(complete, dict)

        status, events = self.request("GET", f"/api/v1/sessions/{session['id']}/events")
        self.assertEqual(status, 200)
        self.assertIsInstance(events, dict)
        event_types = [event["type"] for event in events["data"]]
        self.assertIn("tool.execution_queued", event_types)
        self.assertIn("tool.execution_started", event_types)
        self.assertIn("tool.result", event_types)
        self.assertIn("tool.execution_completed", event_types)

    def test_connector_tools_execute_only_after_approval_and_worker_claim(self) -> None:
        connector_server, connector_thread, connector_records = self.start_fake_connector()
        connector_base = f"http://127.0.0.1:{connector_server.server_port}"
        try:
            status, agent = self.request(
                "POST",
                "/api/v1/agents",
                {"name": "Connector agent", "kernel": {"id": "codex-cli-local"}},
            )
            self.assertEqual(status, 201)
            self.assertIsInstance(agent, dict)
            status, session = self.request("POST", "/api/v1/sessions", {"agent": {"id": agent["id"]}})
            self.assertEqual(status, 201)
            self.assertIsInstance(session, dict)

            status, dify = self.request(
                "POST",
                "/api/v1/integrations",
                {
                    "provider": "dify",
                    "name": "Dify local fake",
                    "base_url": f"{connector_base}/v1",
                    "token": "dify-secret",
                },
            )
            self.assertEqual(status, 201)
            self.assertIsInstance(dify, dict)
            self.assertEqual(dify["status"], "configured")
            self.assertNotIn("dify-secret", json.dumps(dify))

            status, _ = self.request(
                "POST",
                f"/api/v1/sessions/{session['id']}/events",
                {
                    "type": "tool.requested",
                    "payload": {
                        "tool": "integration.dify.chat",
                        "args": {
                            "integration_id": dify["id"],
                            "query": "hello from dify",
                            "inputs": {"source": "unit-test"},
                            "user": "test-user",
                        },
                    },
                },
            )
            self.assertEqual(status, 201)
            self.assertEqual(connector_records, [])

            status, pending = self.request("GET", f"/api/v1/sessions/{session['id']}/pending-actions")
            self.assertEqual(status, 200)
            self.assertIsInstance(pending, dict)
            dify_action = next(
                item
                for item in pending["data"]
                if item["tool"] == "integration.dify.chat" and item["status"] == "pending"
            )
            status, approved = self.request(
                "POST",
                f"/api/v1/sessions/{session['id']}/pending-actions/{dify_action['id']}/resolve",
                {"decision": "approve", "reason": "unit test connector"},
            )
            self.assertEqual(status, 200)
            self.assertIsInstance(approved, dict)
            self.assertEqual(approved["status"], "approved")
            self.assertEqual(connector_records, [])

            status, _ = self.request("POST", "/api/v1/workers", {"id": "worker_connector_1", "name": "Connector worker"})
            self.assertEqual(status, 201)
            status, claim = self.request(
                "POST",
                "/api/v1/workers/worker_connector_1/claim",
                {"lease_seconds": 60},
            )
            self.assertEqual(status, 200)
            self.assertIsInstance(claim, dict)
            self.assertEqual(claim["run"]["trigger_source"], "tool:integration.dify.chat")

            status, executed = self.request(
                "POST",
                f"/api/v1/workers/worker_connector_1/runs/{claim['run']['id']}/tools/execute",
                {"lease_token": claim["run"]["lease_token"], "action_id": dify_action["id"]},
            )
            self.assertEqual(status, 200)
            self.assertIsInstance(executed, dict)
            self.assertEqual(executed["action"]["status"], "executed")
            self.assertNotIn("dify-secret", json.dumps(executed))
            self.assertEqual(len(connector_records), 1)
            self.assertEqual(connector_records[0]["path"], "/v1/chat-messages")
            self.assertEqual(connector_records[0]["headers"]["Authorization"], "Bearer dify-secret")
            self.assertEqual(connector_records[0]["body"]["query"], "hello from dify")
            self.assertEqual(connector_records[0]["body"]["inputs"], {"source": "unit-test"})

            status, complete = self.request(
                "POST",
                f"/api/v1/workers/worker_connector_1/runs/{claim['run']['id']}/complete",
                {
                    "lease_token": claim["run"]["lease_token"],
                    "status": "succeeded",
                    "result": {"tool_action_id": dify_action["id"]},
                },
            )
            self.assertEqual(status, 200)
            self.assertIsInstance(complete, dict)

            status, feishu = self.request(
                "POST",
                "/api/v1/integrations",
                {
                    "provider": "feishu",
                    "name": "Feishu local fake",
                    "base_url": connector_base,
                    "token": "feishu-secret",
                },
            )
            self.assertEqual(status, 201)
            self.assertIsInstance(feishu, dict)
            self.assertNotIn("feishu-secret", json.dumps(feishu))

            status, _ = self.request(
                "POST",
                f"/api/v1/sessions/{session['id']}/events",
                {
                    "type": "tool.requested",
                    "payload": {
                        "tool": "integration.feishu.message",
                        "args": {
                            "integration_id": feishu["id"],
                            "receive_id_type": "open_id",
                            "receive_id": "ou_test",
                            "content": "hello from feishu",
                        },
                    },
                },
            )
            self.assertEqual(status, 201)
            self.assertEqual(len(connector_records), 1)

            status, pending = self.request("GET", f"/api/v1/sessions/{session['id']}/pending-actions")
            self.assertEqual(status, 200)
            self.assertIsInstance(pending, dict)
            feishu_action = next(
                item
                for item in pending["data"]
                if item["tool"] == "integration.feishu.message" and item["status"] == "pending"
            )
            status, approved = self.request(
                "POST",
                f"/api/v1/sessions/{session['id']}/pending-actions/{feishu_action['id']}/resolve",
                {"decision": "approve", "reason": "unit test connector"},
            )
            self.assertEqual(status, 200)
            self.assertIsInstance(approved, dict)
            self.assertEqual(approved["status"], "approved")
            self.assertEqual(len(connector_records), 1)

            status, claim = self.request(
                "POST",
                "/api/v1/workers/worker_connector_1/claim",
                {"lease_seconds": 60},
            )
            self.assertEqual(status, 200)
            self.assertIsInstance(claim, dict)
            self.assertEqual(claim["run"]["trigger_source"], "tool:integration.feishu.message")

            status, executed = self.request(
                "POST",
                f"/api/v1/workers/worker_connector_1/runs/{claim['run']['id']}/tools/execute",
                {"lease_token": claim["run"]["lease_token"], "action_id": feishu_action["id"]},
            )
            self.assertEqual(status, 200)
            self.assertIsInstance(executed, dict)
            self.assertNotIn("feishu-secret", json.dumps(executed))
            self.assertEqual(len(connector_records), 2)
            self.assertEqual(connector_records[1]["path"], "/open-apis/im/v1/messages")
            self.assertEqual(connector_records[1]["query"], {"receive_id_type": ["open_id"]})
            self.assertEqual(connector_records[1]["headers"]["Authorization"], "Bearer feishu-secret")
            self.assertEqual(connector_records[1]["body"]["receive_id"], "ou_test")
            self.assertEqual(json.loads(connector_records[1]["body"]["content"]), {"text": "hello from feishu"})
        finally:
            connector_server.shutdown()
            connector_thread.join(timeout=2)
            connector_server.server_close()


    def test_module_facades_and_openapi_schema_contract(self) -> None:
        from cloudagent_platform.app import Runtime as AppRuntime
        from cloudagent_platform.app import Store as AppStore
        from cloudagent_platform.app import current_openapi, make_handler as app_make_handler

        self.assertIs(AppRuntime, Runtime)
        self.assertIs(AppStore, Store)
        self.assertIs(app_make_handler, make_handler)

        openapi = current_openapi()
        self.assertEqual(openapi["openapi"], "3.1.0")
        paths = openapi["paths"]
        self.assertIn("/api/v1/permission-profiles", paths)
        self.assertIn("/api/v1/sandbox-profiles", paths)
        self.assertIn("/api/v1/vaults", paths)
        self.assertIn("/api/v1/vaults/{vault_id}/credentials", paths)
        self.assertIn("/api/v1/workers/{worker_id}/runs/{run_id}/lease/renew", paths)
        self.assertIn("/api/v1/workers/{worker_id}/runs/{run_id}/complete", paths)
        self.assertIn("/api/v1/webhooks/{provider}/{integration_id}", paths)

        components = openapi["components"]
        schemas = components["schemas"]
        for schema_name in [
            "Error",
            "ListResponse",
            "PermissionProfile",
            "SandboxProfile",
            "Worker",
            "JobRun",
            "Integration",
            "Vault",
            "VaultCredential",
            "PendingAction",
            "WorkerLeaseRequest",
            "WorkerCompleteRequest",
        ]:
            self.assertIn(schema_name, schemas)
            self.assertNotEqual(schemas[schema_name], {"type": "object"})
        self.assertNotIn("secret_material", json.dumps(schemas["Integration"]))
        self.assertEqual(
            components["securitySchemes"]["WebhookToken"]["name"],
            "X-CloudAgent-Webhook-Token",
        )


if __name__ == "__main__":
    unittest.main()
