from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cloudagent_platform.app import Store
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

    def test_tool_failure_is_terminal_but_lease_requeue_recovers_same_bound_action(self) -> None:
        session = self.create_session()
        action = self.store.create_pending_action(
            session["id"], "turn_test", "artifact.create", {"name": "output.txt", "content": "ok"}, "test",
            status="approved", wait_for_approval=False,
        )
        queued = self.store.queue_tool_action(action, "test", source="test")
        self.store.register_worker({"id": "worker-failure", "name": "Failure worker"}, "test")
        claim = self.store.claim_next_run("worker-failure", "test", lease_seconds=60)
        original_execute = self.store.execute_tool

        def fail_execute(*_args: object, **_kwargs: object) -> dict[str, object]:
            raise RuntimeError("connector failed")

        self.store.execute_tool = fail_execute  # type: ignore[method-assign]
        with self.assertRaisesRegex(RuntimeError, "connector failed"):
            self.store.execute_worker_run_tool(
                "worker-failure", claim["run"]["id"],
                {"lease_token": claim["run"]["lease_token"], "action_id": action["id"]}, "test"
            )
        self.store.execute_tool = original_execute  # type: ignore[method-assign]
        self.assertEqual(self.store.get_pending_action(action["id"])["status"], "failed")

        # A lease expiry—not a tool exception—is the explicit retry path for the same run/action.
        with self.store._lock:
            self.store.conn.execute(
                "UPDATE pending_actions SET status = 'executing' WHERE id = ?", (action["id"],)
            )
            self.store.conn.execute(
                "UPDATE job_runs SET lease_expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", queued["run"]["id"]),
            )
            self.store.conn.commit()
        self.store.requeue_expired_runs("test")
        recovered = self.store.get_pending_action(action["id"])
        self.assertEqual(recovered["status"], "approved")
        self.assertEqual(recovered["execution_run_id"], queued["run"]["id"])
        self.assertIsNone(recovered["execution_lease_generation"])

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
