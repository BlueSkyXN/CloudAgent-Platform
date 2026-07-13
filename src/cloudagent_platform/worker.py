from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from .runtime import LocalPrototypeAdapter, RuntimeContext


@dataclass(frozen=True)
class WorkerConfig:
    base_url: str
    token: str
    worker_id: str
    name: str
    lease_seconds: int = 900
    lease_renew_interval_seconds: float | None = None
    poll_interval_seconds: float = 2.0
    server_execute: bool = False


class WorkerApiError(RuntimeError):
    def __init__(self, status: int, payload: dict[str, Any] | str) -> None:
        self.status = status
        self.payload = payload
        super().__init__(f"Worker API request failed with status {status}: {payload}")


class WorkerClient:
    def __init__(self, config: WorkerConfig) -> None:
        self.config = config

    def register(self) -> dict[str, Any]:
        return self.request(
            "POST",
            "/api/v1/workers",
            {
                "id": self.config.worker_id,
                "name": self.config.name,
                "capabilities": {
                    "http_worker": True,
                    "claim_runs": True,
                    "renew_run_leases": True,
                    "execute_claimed_runs": self.config.server_execute,
                    "worker_side_execution": not self.config.server_execute,
                    "local_prototype_adapter": True,
                },
            },
        )

    def heartbeat(self) -> dict[str, Any]:
        return self.request("POST", f"/api/v1/workers/{self.config.worker_id}/heartbeat", {})

    def claim(self) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/workers/{self.config.worker_id}/claim",
            {"lease_seconds": self.config.lease_seconds},
        )

    def execute(self, run_id: str, lease_token: str) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/workers/{self.config.worker_id}/runs/{run_id}/execute",
            {"lease_token": lease_token},
        )

    def renew_lease(self, run_id: str, lease_token: str) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/workers/{self.config.worker_id}/runs/{run_id}/lease/renew",
            {"lease_token": lease_token, "lease_seconds": self.config.lease_seconds},
        )

    def start_turn(self, run_id: str, lease_token: str) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/workers/{self.config.worker_id}/runs/{run_id}/turn/start",
            {"lease_token": lease_token},
        )

    def append_event(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/workers/{self.config.worker_id}/runs/{run_id}/events",
            payload,
        )

    def create_artifact(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/workers/{self.config.worker_id}/runs/{run_id}/artifacts",
            payload,
        )

    def record_usage(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/workers/{self.config.worker_id}/runs/{run_id}/usage",
            payload,
        )

    def execute_tool(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/workers/{self.config.worker_id}/runs/{run_id}/tools/execute",
            payload,
        )

    def complete(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/api/v1/workers/{self.config.worker_id}/runs/{run_id}/complete",
            payload,
        )

    def request(self, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        url = urljoin(self.config.base_url.rstrip("/") + "/", path.lstrip("/"))
        request = Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Content-Type": "application/json",
                "User-Agent": "cloudagent-platform-worker/0.1",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                return self.decode(response.read())
        except HTTPError as exc:
            raw = exc.read()
            try:
                payload_value = self.decode(raw)
            except ValueError:
                payload_value = raw.decode("utf-8", errors="replace")
            raise WorkerApiError(exc.code, payload_value) from exc

    @staticmethod
    def decode(raw: bytes) -> dict[str, Any]:
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Worker API response must be a JSON object")
        return value


class LeaseRenewer:
    def __init__(self, client: WorkerClient, run_id: str, lease_token: str) -> None:
        self.client = client
        self.run_id = run_id
        self.lease_token = lease_token
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: Exception | None = None

    def interval(self) -> float:
        configured = self.client.config.lease_renew_interval_seconds
        if configured is not None and configured > 0:
            return configured
        return max(1.0, min(30.0, self.client.config.lease_seconds / 3))

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        interval = self.interval()
        while not self._stop.wait(interval):
            try:
                self.client.renew_lease(self.run_id, self.lease_token)
            except Exception as exc:
                self.error = exc
                self._stop.set()
                return


class HttpRuntimeStore:
    def __init__(self, client: WorkerClient, run_id: str, lease_token: str) -> None:
        self.client = client
        self.run_id = run_id
        self.lease_token = lease_token

    def append_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        request_id: str,
        severity: str = "info",
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        return self.client.append_event(
            self.run_id,
            {
                "lease_token": self.lease_token,
                "session_id": session_id,
                "type": event_type,
                "payload": payload,
                "severity": severity,
                "turn_id": turn_id,
            },
        )

    def create_artifact(
        self,
        session_id: str,
        payload: dict[str, Any],
        request_id: str,
        turn_id: str | None = None,
        emit_event: bool = True,
    ) -> dict[str, Any]:
        artifact_payload = dict(payload)
        artifact_payload["turn_id"] = turn_id
        artifact_payload["emit_event"] = emit_event
        artifact_payload["session_id"] = session_id
        artifact_payload["lease_token"] = self.lease_token
        return self.client.create_artifact(self.run_id, artifact_payload)

    def record_usage(
        self,
        session_id: str,
        turn_id: str | None,
        request_id: str,
        run_id: str | None = None,
        worker_id: str | None = None,
        token_input: int = 0,
        token_output: int = 0,
        tool_duration_ms: int = 0,
        sandbox_cpu_ms: int = 0,
        sandbox_memory_peak_mb: int = 0,
        sandbox_disk_read_bytes: int = 0,
        sandbox_disk_write_bytes: int = 0,
        sandbox_network_bytes: int = 0,
    ) -> dict[str, Any]:
        return self.client.record_usage(
            self.run_id,
            {
                "lease_token": self.lease_token,
                "session_id": session_id,
                "turn_id": turn_id,
                "run_id": run_id,
                "worker_id": worker_id,
                "token_input": token_input,
                "token_output": token_output,
                "tool_duration_ms": tool_duration_ms,
                "sandbox_cpu_ms": sandbox_cpu_ms,
                "sandbox_memory_peak_mb": sandbox_memory_peak_mb,
                "sandbox_disk_read_bytes": sandbox_disk_read_bytes,
                "sandbox_disk_write_bytes": sandbox_disk_write_bytes,
                "sandbox_network_bytes": sandbox_network_bytes,
            },
        )


def execute_locally(client: WorkerClient, run: dict[str, Any]) -> dict[str, Any]:
    run_id = run["id"]
    lease_token = run["lease_token"]
    started = client.start_turn(run_id, lease_token)
    turn_id = started["turn_id"]
    adapter = LocalPrototypeAdapter()
    store = HttpRuntimeStore(client, run_id, lease_token)
    renewer = LeaseRenewer(client, run_id, lease_token)
    adapter_result: dict[str, Any]
    final_status = "failed"
    renewer.start()
    try:
        adapter_result = adapter.execute_turn(
            store,
            RuntimeContext(
                session_id=run["session_id"],
                turn_id=turn_id,
                source=run["trigger_source"],
                request_id=f"worker:{client.config.worker_id}:{run_id}",
                adapter_id=adapter.adapter_id,
                kernel_id=adapter.kernel_id,
                run_id=run_id,
                worker_id=client.config.worker_id,
                runtime_policy=started.get("runtime_policy"),
            ),
        )
        final_status = "succeeded" if adapter_result.get("status") == "succeeded" else "failed"
    except Exception as exc:
        adapter_result = {
            "status": "failed",
            "error": exc.__class__.__name__,
            "message": str(exc),
        }
        try:
            store.append_event(
                run["session_id"],
                "worker.execution_error",
                {"error": adapter_result["error"], "message": adapter_result["message"]},
                f"worker:{client.config.worker_id}:{run_id}",
                severity="error",
                turn_id=turn_id,
            )
        finally:
            final_status = "failed"
    finally:
        renewer.stop()
    if renewer.error and final_status == "succeeded":
        adapter_result = {
            "status": "failed",
            "error": renewer.error.__class__.__name__,
            "message": str(renewer.error),
            "stage": "lease_renewal",
            "adapter": adapter_result,
        }
        final_status = "failed"
    completed = client.complete(
        run_id,
        {
            "lease_token": lease_token,
            "status": final_status,
            "turn_id": turn_id,
            "result": {
                "session_id": run["session_id"],
                "completed_by": client.config.worker_id,
                "execution_mode": "worker-local",
                "adapter": adapter_result,
            },
        },
    )
    completed["adapter"] = adapter_result
    completed["turn_id"] = turn_id
    return completed


def run_once(config: WorkerConfig) -> dict[str, Any]:
    client = WorkerClient(config)
    worker = client.register()
    claim = client.claim()
    run = claim.get("run")
    if not run:
        client.heartbeat()
        return {"type": "worker_once", "worker": worker, "claimed": False, "run": None}
    executed = client.execute(run["id"], run["lease_token"]) if config.server_execute else execute_locally(client, run)
    return {
        "type": "worker_once",
        "worker": executed.get("worker") or worker,
        "claimed": True,
        "run": executed["run"],
        "execution": executed,
    }


def run_loop(config: WorkerConfig, max_iterations: int | None = None) -> None:
    client = WorkerClient(config)
    client.register()
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        claim = client.claim()
        run = claim.get("run")
        if run:
            result = client.execute(run["id"], run["lease_token"]) if config.server_execute else execute_locally(client, run)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
            continue
        client.heartbeat()
        time.sleep(config.poll_interval_seconds)


def default_worker_id() -> str:
    return f"worker_{socket.gethostname()}_{os.getpid()}".replace(".", "_")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a CloudAgent Platform HTTP worker.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("CLOUDAGENT_API_URL", "http://127.0.0.1:8080"),
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("CLOUDAGENT_AUTH_TOKEN"),
        required=os.environ.get("CLOUDAGENT_AUTH_TOKEN") is None,
    )
    parser.add_argument("--worker-id", default=os.environ.get("CLOUDAGENT_WORKER_ID") or default_worker_id())
    parser.add_argument("--name", default=os.environ.get("CLOUDAGENT_WORKER_NAME", "CloudAgent HTTP worker"))
    parser.add_argument("--lease-seconds", type=int, default=int(os.environ.get("CLOUDAGENT_WORKER_LEASE_SECONDS", "900")))
    parser.add_argument(
        "--lease-renew-interval",
        type=float,
        default=(
            float(os.environ["CLOUDAGENT_WORKER_LEASE_RENEW_INTERVAL"])
            if os.environ.get("CLOUDAGENT_WORKER_LEASE_RENEW_INTERVAL")
            else None
        ),
        help="Seconds between active run lease renewals. Defaults to min(30, lease_seconds / 3).",
    )
    parser.add_argument("--poll-interval", type=float, default=float(os.environ.get("CLOUDAGENT_WORKER_POLL_INTERVAL", "2")))
    parser.add_argument("--once", action="store_true", help="Claim and execute at most one run, then exit.")
    parser.add_argument("--max-iterations", type=int, default=None)
    parser.add_argument(
        "--server-execute",
        action="store_true",
        help="Use the legacy server-side /execute path instead of worker-local execution.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = WorkerConfig(
        base_url=args.base_url,
        token=args.token,
        worker_id=args.worker_id,
        name=args.name,
        lease_seconds=args.lease_seconds,
        lease_renew_interval_seconds=args.lease_renew_interval,
        poll_interval_seconds=args.poll_interval,
        server_execute=args.server_execute,
    )
    if args.once:
        print(json.dumps(run_once(config), ensure_ascii=False, sort_keys=True), flush=True)
        return
    run_loop(config, max_iterations=args.max_iterations)


if __name__ == "__main__":
    main()
