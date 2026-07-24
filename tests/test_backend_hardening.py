from __future__ import annotations

import json
import socket
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cloudagent_platform.app import Store
from cloudagent_platform.errors import ConnectorRequestError
from cloudagent_platform.http import make_handler
from cloudagent_platform.openapi import current_openapi
from cloudagent_platform.scheduler import Runtime
from cloudagent_platform.showcase import ShowcaseService
from cloudagent_platform.status import sdlc_status_payload


class BackendHardeningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.tmp.name) / "backend-hardening.sqlite3")
        self.store = Store(self.database)

    def tearDown(self) -> None:
        try:
            self.store.close()
        except sqlite3.ProgrammingError:
            pass
        self.tmp.cleanup()

    def create_session(self) -> dict[str, object]:
        agent = self.store.create_agent({"name": "Hardening agent"}, "test")
        environment = self.store.list_environments()[0]
        return self.store.create_session(
            {"agent_id": agent["id"], "environment_id": environment["id"]}, "test"
        )

    def test_same_session_runs_are_claimed_serially_and_requeue_preserves_queue_state(self) -> None:
        session = self.create_session()
        first = self.store.create_run(session["id"], "test", "session:user.message")
        second = self.store.create_run(session["id"], "test", "session:user.message")
        self.store.register_worker({"id": "worker-one", "name": "Worker one"}, "test")
        self.store.register_worker({"id": "worker-two", "name": "Worker two"}, "test")

        first_claim = self.store.claim_next_run("worker-one", "test", lease_seconds=60)
        self.assertEqual(first_claim["run"]["id"], first["id"])
        self.assertIsNone(self.store.claim_next_run("worker-two", "test", lease_seconds=60)["run"])
        self.assertEqual(self.store.get_session(session["id"])["status"], "starting")

        self.store.complete_worker_run(
            "worker-one",
            first["id"],
            {"lease_token": first_claim["run"]["lease_token"], "status": "succeeded", "result": {}},
            "test",
        )
        self.assertEqual(self.store.get_session(session["id"])["status"], "queued")
        second_claim = self.store.claim_next_run("worker-two", "test", lease_seconds=60)
        self.assertEqual(second_claim["run"]["id"], second["id"])

        with self.store._lock:
            self.store.conn.execute(
                "UPDATE job_runs SET lease_expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", second["id"]),
            )
            self.store.conn.commit()
        self.assertEqual(self.store.requeue_expired_runs("test"), 1)
        self.assertEqual(self.store.get_session(session["id"])["status"], "queued")

        retry_claim = self.store.claim_next_run("worker-one", "test", lease_seconds=60)
        self.assertEqual(retry_claim["run"]["id"], second["id"])
        self.assertIsNone(self.store.claim_next_run("worker-two", "test", lease_seconds=60)["run"])

    def test_bootstrap_is_singleton_under_concurrent_calls(self) -> None:
        service = ShowcaseService(self.store)
        barrier = threading.Barrier(3)
        failures: list[BaseException] = []

        def bootstrap() -> None:
            try:
                barrier.wait(timeout=2)
                service.bootstrap("parallel")
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        threads = [threading.Thread(target=bootstrap), threading.Thread(target=bootstrap)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(failures, [])
        self.assertEqual(len(self.store.list_agents()), 1)
        self.assertEqual(len(self.store.list_sessions()), 1)
        self.assertEqual(len(self.store.list_jobs()), 1)
        self.assertEqual(len(self.store.list_workers()), 1)
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_worker_adapter_return_does_not_mark_session_idle_before_run_completion(self) -> None:
        session = self.create_session()
        run = self.store.create_run(session["id"], "test", "session:user.message")
        self.store.register_worker({"id": "worker-adapter", "name": "Adapter worker"}, "test")
        claim = self.store.claim_next_run("worker-adapter", "test", lease_seconds=60)

        class SuccessfulAdapter:
            adapter_id = "test-adapter"
            kernel_id = "test-kernel"

            def execute_turn(self, *_args: object, **_kwargs: object) -> dict[str, str]:
                return {"status": "succeeded"}

        original_adapter = self.store.adapter
        self.store.adapter = SuccessfulAdapter()  # type: ignore[assignment]
        try:
            adapter_result = self.store.run_adapter_turn(
                session["id"], "session:user.message", "test", run_id=run["id"], worker_id="worker-adapter"
            )
            self.assertEqual(self.store.get_session(session["id"])["status"], "running")
            event_types = [event["type"] for event in self.store.list_events(session["id"])]
            self.assertNotIn("session.idle", event_types)
            self.store.complete_worker_run(
                "worker-adapter",
                run["id"],
                {
                    "lease_token": claim["run"]["lease_token"],
                    "status": "succeeded",
                    "result": {"adapter": adapter_result["adapter"]},
                },
                "test",
            )
        finally:
            self.store.adapter = original_adapter
        self.assertEqual(self.store.get_session(session["id"])["status"], "idle")
        self.assertIn("session.idle", [event["type"] for event in self.store.list_events(session["id"])])

    def test_tool_queue_is_bound_once_and_execution_cas_prevents_replay(self) -> None:
        session = self.create_session()
        action = self.store.create_pending_action(
            session["id"], "turn_test", "artifact.create", {"name": "output.txt", "content": "ok"}, "test",
            status="approved", wait_for_approval=False,
        )
        queued = self.store.queue_tool_action(action, "test", source="test")
        repeated = self.store.queue_tool_action(self.store.get_pending_action(action["id"]), "test", source="retry")
        self.assertEqual(repeated["run"]["id"], queued["run"]["id"])
        self.assertEqual(self.store.get_pending_action(action["id"])["execution_run_id"], queued["run"]["id"])

        self.store.register_worker({"id": "worker-tool", "name": "Tool worker"}, "test")
        claim = self.store.claim_next_run("worker-tool", "test", lease_seconds=60)
        token = claim["run"]["lease_token"]
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def slow_execute(*_args: object, **_kwargs: object) -> dict[str, object]:
            calls.append("execute")
            entered.set()
            release.wait(timeout=2)
            return {"ok": True}

        original_execute = self.store.execute_tool
        self.store.execute_tool = slow_execute  # type: ignore[method-assign]
        first_result: list[object] = []

        def execute_once() -> None:
            try:
                first_result.append(
                    self.store.execute_worker_run_tool(
                        "worker-tool", claim["run"]["id"], {"lease_token": token, "action_id": action["id"]}, "test"
                    )
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                first_result.append(exc)

        thread = threading.Thread(target=execute_once)
        thread.start()
        self.assertTrue(entered.wait(timeout=2))
        with self.assertRaisesRegex(ValueError, "already executing"):
            self.store.execute_worker_run_tool(
                "worker-tool", claim["run"]["id"], {"lease_token": token, "action_id": action["id"]}, "test"
            )
        release.set()
        thread.join(timeout=2)
        self.store.execute_tool = original_execute  # type: ignore[method-assign]
        self.assertEqual(calls, ["execute"])
        self.assertEqual(self.store.get_pending_action(action["id"])["status"], "executed")
        self.assertEqual(len(first_result), 1)

        concurrent_action = self.store.create_pending_action(
            session["id"], "turn_concurrent", "artifact.create", {"name": "parallel", "content": "ok"}, "test",
            status="approved", wait_for_approval=False,
        )
        queue_barrier = threading.Barrier(3)
        queued_runs: list[str] = []
        queue_failures: list[BaseException] = []

        def queue_once() -> None:
            try:
                queue_barrier.wait(timeout=2)
                response = self.store.queue_tool_action(concurrent_action, "test", source="parallel")
                queued_runs.append(response["run"]["id"])
            except BaseException as exc:  # pragma: no cover - asserted below
                queue_failures.append(exc)

        queue_threads = [threading.Thread(target=queue_once), threading.Thread(target=queue_once)]
        for queue_thread in queue_threads:
            queue_thread.start()
        queue_barrier.wait(timeout=2)
        for queue_thread in queue_threads:
            queue_thread.join(timeout=2)
        self.assertEqual(queue_failures, [])
        self.assertEqual(len(queued_runs), 2)
        self.assertEqual(queued_runs[0], queued_runs[1])
        self.assertEqual(
            self.store.get_pending_action(concurrent_action["id"])["execution_run_id"], queued_runs[0]
        )

    def test_tool_run_cannot_bypass_bound_action_through_generic_worker_paths(self) -> None:
        session = self.create_session()
        action = self.store.create_pending_action(
            session["id"],
            "turn_no_bypass",
            "artifact.create",
            {"name": "protected.txt", "content": "ok"},
            "test",
            status="approved",
            wait_for_approval=False,
        )
        queued = self.store.queue_tool_action(action, "test", source="test")
        self.store.register_worker({"id": "worker-no-bypass", "name": "No bypass worker"}, "test")
        claim = self.store.claim_next_run("worker-no-bypass", "test", lease_seconds=60)
        lease_token = claim["run"]["lease_token"]

        with self.assertRaisesRegex(ValueError, "tools/execute"):
            self.store.start_worker_run_turn("worker-no-bypass", queued["run"]["id"], lease_token, "test")
        with self.assertRaisesRegex(ValueError, "tools/execute"):
            self.store.execute_claimed_run("worker-no-bypass", queued["run"]["id"], lease_token, "test")
        with self.assertRaisesRegex(ValueError, "matching terminal state"):
            self.store.complete_worker_run(
                "worker-no-bypass",
                queued["run"]["id"],
                {"lease_token": lease_token, "status": "succeeded", "result": {}},
                "test",
            )

        self.assertEqual(self.store.get_pending_action(action["id"])["status"], "approved")
        self.assertEqual(self.store.get_run(queued["run"]["id"])["status"], "running")

    def test_orphaned_tool_binding_is_recovered_by_creating_its_bound_run(self) -> None:
        session = self.create_session()
        action = self.store.create_pending_action(
            session["id"], "turn_orphan", "artifact.create", {"name": "orphan.txt", "content": "ok"}, "test",
            status="approved", wait_for_approval=False,
        )
        orphan_run_id = "run_orphan_binding"
        with self.store._lock:
            self.store.conn.execute(
                "UPDATE pending_actions SET execution_run_id = ? WHERE id = ?", (orphan_run_id, action["id"])
            )
            self.store.conn.commit()

        recovered = self.store.resolve_pending_action(
            session["id"], action["id"], {"decision": "approve"}, "test"
        )
        self.assertEqual(recovered["execution_run_id"], orphan_run_id)
        self.assertEqual(self.store.get_run(orphan_run_id)["trigger_source"], "tool:artifact.create")
        self.assertEqual(self.store.get_pending_action(action["id"])["execution_run_id"], orphan_run_id)
        self.assertIn(
            "tool.execution_queued",
            [event["type"] for event in self.store.list_events(session["id"])],
        )

    def test_inflight_tool_lease_expiry_fails_without_replaying_side_effect(self) -> None:
        session = self.create_session()
        action = self.store.create_pending_action(
            session["id"], "turn_test", "artifact.create", {"name": "output.txt", "content": "ok"}, "test",
            status="approved", wait_for_approval=False,
        )
        queued = self.store.queue_tool_action(action, "test", source="test")
        self.store.register_worker({"id": "worker-failure", "name": "Failure worker"}, "test")
        claim = self.store.claim_next_run("worker-failure", "test", lease_seconds=60)
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []
        original_execute = self.store.execute_tool

        def slow_execute(*_args: object, **_kwargs: object) -> dict[str, object]:
            calls.append("side-effect")
            entered.set()
            release.wait(timeout=2)
            return {"ok": True}

        self.store.execute_tool = slow_execute  # type: ignore[method-assign]
        outcomes: list[BaseException | dict[str, object]] = []

        def execute_once() -> None:
            try:
                outcomes.append(
                    self.store.execute_worker_run_tool(
                        "worker-failure", queued["run"]["id"],
                        {"lease_token": claim["run"]["lease_token"], "action_id": action["id"]}, "test"
                    )
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                outcomes.append(exc)

        execution = threading.Thread(target=execute_once)
        execution.start()
        self.assertTrue(entered.wait(timeout=2))
        with self.store._lock:
            self.store.conn.execute(
                "UPDATE job_runs SET lease_expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", queued["run"]["id"]),
            )
            self.store.conn.commit()
        self.assertEqual(self.store.requeue_expired_runs("test"), 1)
        release.set()
        execution.join(timeout=2)
        self.store.execute_tool = original_execute  # type: ignore[method-assign]

        self.assertEqual(calls, ["side-effect"])
        self.assertEqual(self.store.get_pending_action(action["id"])["status"], "failed")
        self.assertEqual(self.store.get_run(queued["run"]["id"])["status"], "failed")
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], RuntimeError)

    def test_expired_tool_run_with_terminal_action_is_converged_not_requeued(self) -> None:
        for action_status, expected_run_status in (("executed", "succeeded"), ("failed", "failed"), ("rejected", "failed")):
            with self.subTest(action_status=action_status):
                session = self.create_session()
                action = self.store.create_pending_action(
                    session["id"], f"turn_{action_status}", "artifact.create", {"name": "output.txt", "content": "ok"}, "test",
                    status="approved", wait_for_approval=False,
                )
                queued = self.store.queue_tool_action(action, "test", source="test")
                worker_id = f"worker-terminal-{action_status}"
                self.store.register_worker({"id": worker_id, "name": "Terminal worker"}, "test")
                claim = self.store.claim_next_run(worker_id, "test", lease_seconds=60)
                with self.store._lock:
                    self.store.conn.execute(
                        "UPDATE pending_actions SET status = ?, execution_lease_generation = NULL WHERE id = ?",
                        (action_status, action["id"]),
                    )
                    self.store.conn.execute(
                        "UPDATE job_runs SET lease_expires_at = ? WHERE id = ?",
                        ("2000-01-01T00:00:00+00:00", claim["run"]["id"]),
                    )
                    self.store.conn.commit()

                self.assertEqual(self.store.requeue_expired_runs("test"), 1)
                self.assertEqual(self.store.get_run(queued["run"]["id"])["status"], expected_run_status)
                self.assertEqual(self.store.get_pending_action(action["id"])["status"], action_status)

    def test_expired_tool_run_without_a_bound_action_fails_closed(self) -> None:
        session = self.create_session()
        action = self.store.create_pending_action(
            session["id"], "turn_missing_action", "artifact.create", {"name": "output.txt", "content": "ok"}, "test",
            status="approved", wait_for_approval=False,
        )
        queued = self.store.queue_tool_action(action, "test", source="test")
        self.store.register_worker({"id": "worker-missing-action", "name": "Missing action worker"}, "test")
        claim = self.store.claim_next_run("worker-missing-action", "test", lease_seconds=60)
        with self.store._lock:
            self.store.conn.execute("DELETE FROM pending_actions WHERE id = ?", (action["id"],))
            self.store.conn.execute(
                "UPDATE job_runs SET lease_expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", claim["run"]["id"]),
            )
            self.store.conn.commit()

        self.assertEqual(self.store.requeue_expired_runs("test"), 1)
        self.assertEqual(self.store.get_run(queued["run"]["id"])["status"], "failed")
        event = next(
            item for item in self.store.list_events(session["id"]) if item["type"] == "worker.lease_expired"
        )
        self.assertEqual(event["payload"]["recovery_action"], "invalid_tool_run_failed")

    def test_connector_failures_and_target_controls_are_fail_closed(self) -> None:
        requests: list[dict[str, object]] = []
        redirected_requests: list[dict[str, object]] = []

        class DestinationHandler(BaseHTTPRequestHandler):
            def record(self) -> None:
                redirected_requests.append(
                    {"method": self.command, "authorization": self.headers.get("Authorization")}
                )
                self.send_response(200)
                self.end_headers()

            do_POST = record
            do_GET = record

            def log_message(self, _format: str, *_args: object) -> None:
                return

        destination = ThreadingHTTPServer(("127.0.0.1", 0), DestinationHandler)
        destination_thread = threading.Thread(target=destination.serve_forever, daemon=True)
        destination_thread.start()

        class ConnectorHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                requests.append({"path": self.path, "authorization": self.headers.get("Authorization")})
                if self.path == "/redirect/chat-messages":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{destination.server_port}/received")
                    body = b'{"error":"redirect"}'
                elif self.path == "/oversized/chat-messages":
                    self.send_response(200)
                    body = b"x" * 64
                elif self.path == "/fallback/chat-messages":
                    self.send_response(200)
                    body = b'{"ok":true}'
                else:
                    self.send_response(500)
                    body = b'{"error":"upstream failure"}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        connector = ThreadingHTTPServer(("127.0.0.1", 0), ConnectorHandler)
        connector_thread = threading.Thread(target=connector.serve_forever, daemon=True)
        connector_thread.start()
        try:
            unsafe_store = Store(str(Path(self.tmp.name) / "unsafe-connectors.sqlite3"), allow_unsafe_connector_urls_for_tests=True)
            self.addCleanup(unsafe_store.close)
            integration = unsafe_store.create_integration(
                {"provider": "dify", "name": "Failure connector", "base_url": f"http://127.0.0.1:{connector.server_port}", "secret": "connector-secret"},
                "test",
            )
            with self.assertRaises(ConnectorRequestError) as failure:
                unsafe_store.invoke_dify_chat({"integration_id": integration["id"], "query": "hello"})
            self.assertEqual(
                failure.exception.status_code,
                500,
                msg=f"cause={failure.exception.__cause__!r} summary={failure.exception.response_summary!r}",
            )
            self.assertNotIn("connector-secret", str(failure.exception))

            agent = unsafe_store.create_agent({"name": "Connector failure agent"}, "test")
            session = unsafe_store.create_session(
                {"agent_id": agent["id"], "environment_id": unsafe_store.list_environments()[0]["id"]}, "test"
            )
            action = unsafe_store.create_pending_action(
                session["id"],
                "turn_connector_failure",
                "integration.dify.chat",
                {"integration_id": integration["id"], "query": "worker failure"},
                "test",
                status="approved",
                wait_for_approval=False,
            )
            queued = unsafe_store.queue_tool_action(action, "test", source="test")
            unsafe_store.register_worker({"id": "worker-http-500", "name": "HTTP failure worker"}, "test")
            claim = unsafe_store.claim_next_run("worker-http-500", "test", lease_seconds=60)
            with self.assertRaises(ConnectorRequestError):
                unsafe_store.execute_worker_run_tool(
                    "worker-http-500",
                    queued["run"]["id"],
                    {"lease_token": claim["run"]["lease_token"], "action_id": action["id"]},
                    "test",
                )
            self.assertEqual(unsafe_store.get_pending_action(action["id"])["status"], "failed")
            self.assertNotEqual(unsafe_store.get_pending_action(action["id"])["status"], "executed")
            self.assertEqual(unsafe_store.get_run(queued["run"]["id"])["status"], "failed")

            redirect = unsafe_store.create_integration(
                {"provider": "dify", "name": "Redirect connector", "base_url": f"http://127.0.0.1:{connector.server_port}/redirect", "secret": "redirect-secret"},
                "test",
            )
            with self.assertRaises(ConnectorRequestError) as redirect_failure:
                unsafe_store.invoke_dify_chat({"integration_id": redirect["id"], "query": "hello"})
            self.assertEqual(redirect_failure.exception.status_code, 302)
            self.assertEqual(redirected_requests, [])

            tiny_store = Store(
                str(Path(self.tmp.name) / "tiny-connectors.sqlite3"),
                max_content_bytes=32,
                allow_unsafe_connector_urls_for_tests=True,
            )
            self.addCleanup(tiny_store.close)
            oversized = tiny_store.create_integration(
                {"provider": "dify", "name": "Oversized connector", "base_url": f"http://127.0.0.1:{connector.server_port}/oversized", "secret": "oversized-secret"},
                "test",
            )
            with self.assertRaises(ConnectorRequestError) as oversized_failure:
                tiny_store.invoke_dify_chat({"integration_id": oversized["id"], "query": "hello"})
            self.assertEqual(oversized_failure.exception.status_code, 200)
            self.assertEqual(oversized_failure.exception.response_summary, "response body exceeds maximum size")

            original_getaddrinfo = socket.getaddrinfo

            def fallback_getaddrinfo(host: str, port: int, *args: object, **kwargs: object) -> object:
                if host == "fallback.example":
                    return [
                        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", port)),
                        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
                    ]
                return original_getaddrinfo(host, port, *args, **kwargs)

            fallback = unsafe_store.create_integration(
                {"provider": "dify", "name": "Fallback connector", "base_url": f"http://fallback.example:{connector.server_port}/fallback", "secret": "fallback-secret"},
                "test",
            )
            with patch("cloudagent_platform.app.socket.getaddrinfo", side_effect=fallback_getaddrinfo):
                fallback_result = unsafe_store.invoke_dify_chat({"integration_id": fallback["id"], "query": "hello"})
            self.assertTrue(fallback_result["ok"])

            safe_store = Store(str(Path(self.tmp.name) / "safe-connectors.sqlite3"))
            self.addCleanup(safe_store.close)
            blocked = safe_store.create_integration(
                {"provider": "dify", "name": "Blocked connector", "base_url": f"http://127.0.0.1:{connector.server_port}", "secret": "blocked-secret"},
                "test",
            )
            with self.assertRaisesRegex(ValueError, "restricted"):
                safe_store.invoke_dify_chat({"integration_id": blocked["id"], "query": "hello"})
            with patch(
                "cloudagent_platform.app.socket.getaddrinfo",
                return_value=[
                    (2, 1, 6, "", ("93.184.216.34", 443)),
                    (2, 1, 6, "", ("10.0.0.8", 443)),
                ],
            ):
                with self.assertRaisesRegex(ValueError, "restricted"):
                    safe_store.resolve_connector_target("https://mixed-address.example")
            self.assertEqual(len(requests), 5)
        finally:
            connector.shutdown()
            connector_thread.join(timeout=2)
            connector.server_close()
            destination.shutdown()
            destination_thread.join(timeout=2)
            destination.server_close()

    def test_webhook_credential_can_be_registered_without_outbound_base_url(self) -> None:
        webhook = self.store.create_integration({"provider": "webhook", "name": "Inbound webhook"}, "test")
        self.assertEqual(webhook["status"], "credential_required")
        registered = self.store.register_integration_credential(webhook["id"], {"secret": "inbound-secret"}, "test")
        self.assertEqual(registered["status"], "configured")
        self.assertEqual(registered["credential_status"], "registered")

    def test_integration_secret_is_memory_only_and_reregisterable(self) -> None:
        integration = self.store.create_integration(
            {"provider": "dify", "name": "Dify", "base_url": "https://dify.example/v1", "secret": "db-secret"},
            "test",
        )
        self.assertEqual(integration["status"], "configured")
        with self.assertRaisesRegex(ValueError, "http or https"):
            self.store.create_integration(
                {"provider": "dify", "name": "Invalid", "base_url": "ftp://dify.example"}, "test"
            )
        with self.assertRaisesRegex(ValueError, "userinfo"):
            self.store.create_integration(
                {"provider": "dify", "name": "Invalid", "base_url": "https://user:pass@dify.example"}, "test"
            )
        with self.store._lock:
            columns = {
                row["name"] for row in self.store.conn.execute("PRAGMA table_info(integrations)").fetchall()
            }
            self.assertNotIn("secret_material", columns)
            persisted = self.store.conn.execute("SELECT secret_ref FROM integrations WHERE id = ?", (integration["id"],)).fetchone()
        self.assertNotIn("db-secret", str(dict(persisted)))
        self.store.close()
        self.store = Store(self.database)
        after_restart = self.store.get_integration(integration["id"])
        self.assertEqual(after_restart["status"], "credential_required")
        self.assertEqual(after_restart["credential_status"], "registration_required")
        self.assertIsNone(self.store.get_integration_secret_material(integration["id"]))
        registered = self.store.register_integration_credential(integration["id"], {"secret": "new-secret"}, "test")
        self.assertEqual(registered["status"], "configured")
        self.assertNotIn("new-secret", json.dumps(registered))

    def test_legacy_integration_plaintext_is_dropped_and_vacuumed(self) -> None:
        integration = self.store.create_integration(
            {"provider": "dify", "name": "Legacy Dify", "base_url": "https://dify.example/v1"}, "test"
        )
        legacy_secret = "legacy-integration-secret-unique"
        with self.store._lock:
            self.store.conn.execute("ALTER TABLE integrations ADD COLUMN secret_material TEXT")
            self.store.conn.execute(
                "UPDATE integrations SET secret_material = ? WHERE id = ?", (legacy_secret, integration["id"])
            )
            self.store.conn.commit()
        self.store.close()
        self.store = Store(self.database)
        with self.store._lock:
            columns = {
                row["name"] for row in self.store.conn.execute("PRAGMA table_info(integrations)").fetchall()
            }
        self.assertNotIn("secret_material", columns)
        self.assertNotIn(legacy_secret.encode("utf-8"), Path(self.database).read_bytes())
        self.assertEqual(self.store.get_integration(integration["id"])["status"], "credential_required")

    def test_multiple_pending_approvals_keep_session_waiting_until_last_resolution(self) -> None:
        session = self.create_session()
        first = self.store.create_pending_action(
            session["id"], "turn_one", "artifact.create", {"name": "one", "content": "one"}, "test"
        )
        second = self.store.create_pending_action(
            session["id"], "turn_two", "artifact.create", {"name": "two", "content": "two"}, "test"
        )
        self.store.resolve_pending_action(session["id"], first["id"], {"decision": "approve"}, "test")
        self.assertEqual(self.store.get_session(session["id"])["status"], "waiting_approval")
        self.store.register_worker({"id": "waiting-worker", "name": "Waiting worker"}, "test")
        self.assertIsNone(self.store.claim_next_run("waiting-worker", "test", lease_seconds=60)["run"])
        self.store.resolve_pending_action(session["id"], second["id"], {"decision": "reject"}, "test")
        self.assertEqual(self.store.get_session(session["id"])["status"], "queued")

    def test_auth_and_openapi_security_contracts(self) -> None:
        openapi = current_openapi()
        self.assertEqual(sdlc_status_payload()["status"], "company-showcase-release")
        for path in ("/_ops/healthz", "/_ops/readyz", "/openapi.json", "/api/v1/sdlc/status"):
            self.assertEqual(openapi["paths"][path]["get"]["security"], [])
        webhook = openapi["paths"]["/api/v1/webhooks/{provider}/{integration_id}"]["post"]
        self.assertEqual(webhook["security"], [{"WebhookToken": []}])
        credential = openapi["paths"]["/api/v1/integrations/{integration_id}/credential"]["post"]
        self.assertTrue(credential["requestBody"]["required"])
        self.assertTrue(
            credential["requestBody"]["content"]["application/json"]["schema"]
            ["$ref"].endswith("IntegrationCredentialRequest")
        )

        runtime = Runtime(self.store, "test-token")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(runtime))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_port}"
            with urlopen(base_url + "/_ops/healthz", timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
            request = Request(base_url + "/api/v1/agents", method="GET")
            with self.assertRaises(HTTPError) as failure:
                urlopen(request, timeout=2)
            self.assertEqual(failure.exception.code, 401)

            integration = self.store.create_integration(
                {"provider": "dify", "name": "Dify", "base_url": "https://dify.example/v1"}, "test"
            )
            credential_request = Request(
                base_url + f"/api/v1/integrations/{integration['id']}/credential",
                data=json.dumps({"secret": "process-only"}).encode("utf-8"),
                method="POST",
                headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
            )
            with urlopen(credential_request, timeout=2) as response:
                registered = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(registered["status"], "configured")
            self.assertNotIn("process-only", json.dumps(registered))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()
            runtime.stop()


if __name__ == "__main__":
    unittest.main()
