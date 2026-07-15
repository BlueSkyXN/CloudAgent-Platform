from __future__ import annotations

import hmac
import json
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .console import get_console_asset
from .errors import PayloadTooLargeError
from .openapi import current_openapi
from .scheduler import Runtime
from .utils import json_dumps, new_id
from .status import sdlc_status_payload


def make_handler(runtime: Runtime) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"CloudAgentPlatform/{__version__}"

        def do_OPTIONS(self) -> None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_common_headers()
            self.end_headers()

        def do_GET(self) -> None:
            self.route("GET")

        def do_POST(self) -> None:
            self.route("POST")

        def route(self, method: str) -> None:
            request_id = self.headers.get("X-Request-Id") or new_id("req")
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") if parsed.path != "/" else parsed.path
            query = parse_qs(parsed.query)
            parts = [part for part in path.split("/") if part]
            is_webhook_trigger = (
                method == "POST"
                and len(parts) == 5
                and parts[:3] == ["api", "v1", "webhooks"]
            )

            try:
                console_asset = get_console_asset(path) if method == "GET" else None
                if console_asset is not None:
                    self.respond_console_asset(HTTPStatus.OK, console_asset)
                    return
                if method == "GET" and path == "/_ops/healthz":
                    self.respond_json(
                        HTTPStatus.OK,
                        {
                            "status": "ok",
                            "service": "CloudAgent-Platform",
                            "started_at": runtime.started_at,
                        },
                    )
                    return
                if method == "GET" and path == "/_ops/readyz":
                    self.respond_json(
                        HTTPStatus.OK,
                        {
                            "status": "ready",
                            "service": "CloudAgent-Platform",
                            "database": Path(runtime.store.path).name,
                        },
                    )
                    return
                if method == "GET" and path == "/openapi.json":
                    self.respond_json(HTTPStatus.OK, current_openapi())
                    return
                if method == "GET" and path == "/api/v1/sdlc/status":
                    self.respond_json(HTTPStatus.OK, sdlc_status_payload())
                    return

                if path.startswith("/api/v1/") and not is_webhook_trigger:
                    self.require_auth()

                if method == "GET" and path == "/api/v1/system/info":
                    self.respond_json(
                        HTTPStatus.OK,
                        {
                            "type": "cloudagent.system_info",
                            "service": "CloudAgent-Platform",
                            "version": __version__,
                            "mode": "local-prototype",
                            "started_at": runtime.started_at,
                            "auth": "bearer",
                            "database": Path(runtime.store.path).name,
                            "runtime_adapter": runtime.store.adapter.manifest(),
                            "kernels": runtime.store.list_kernels(),
                            "permission_profiles": runtime.store.list_permission_profiles(),
                            "sandbox_profiles": runtime.store.list_sandbox_profiles(),
                        },
                    )
                    return

                if method == "GET" and path == "/api/v1/permission-profiles":
                    self.respond_list(runtime.store.list_permission_profiles())
                    return
                if (
                    len(parts) == 4
                    and parts[:3] == ["api", "v1", "permission-profiles"]
                    and method == "GET"
                ):
                    self.respond_json(HTTPStatus.OK, runtime.store.get_permission_profile(parts[3]))
                    return
                if method == "GET" and path == "/api/v1/sandbox-profiles":
                    self.respond_list(runtime.store.list_sandbox_profiles())
                    return
                if (
                    len(parts) == 4
                    and parts[:3] == ["api", "v1", "sandbox-profiles"]
                    and method == "GET"
                ):
                    self.respond_json(HTTPStatus.OK, runtime.store.get_sandbox_profile(parts[3]))
                    return

                if method == "GET" and path == "/api/v1/kernels":
                    self.respond_list(runtime.store.list_kernels())
                    return
                if len(parts) == 4 and parts[:3] == ["api", "v1", "kernels"] and method == "GET":
                    self.respond_json(HTTPStatus.OK, runtime.store.get_kernel(parts[3]))
                    return
                if (
                    len(parts) == 5
                    and parts[:3] == ["api", "v1", "kernels"]
                    and parts[4] == "probe"
                    and method == "POST"
                ):
                    self.respond_json(HTTPStatus.OK, runtime.store.probe_kernel(parts[3]))
                    return

                if method == "GET" and path == "/api/v1/agents":
                    self.respond_list(runtime.store.list_agents())
                    return
                if method == "POST" and path == "/api/v1/agents":
                    self.respond_json(
                        HTTPStatus.CREATED,
                        runtime.store.create_agent(self.read_json(), request_id),
                    )
                    return

                if method == "GET" and path == "/api/v1/environments":
                    self.respond_list(runtime.store.list_environments())
                    return
                if method == "POST" and path == "/api/v1/environments":
                    self.respond_json(
                        HTTPStatus.CREATED,
                        runtime.store.create_environment(self.read_json(), request_id),
                    )
                    return

                if method == "GET" and path == "/api/v1/sessions":
                    self.respond_list(runtime.store.list_sessions())
                    return
                if method == "POST" and path == "/api/v1/sessions":
                    self.respond_json(
                        HTTPStatus.CREATED,
                        runtime.store.create_session(self.read_json(), request_id),
                    )
                    return

                if len(parts) >= 4 and parts[:3] == ["api", "v1", "sessions"]:
                    session_id = parts[3]
                    if len(parts) == 4 and method == "GET":
                        self.respond_json(HTTPStatus.OK, runtime.store.get_session(session_id))
                        return
                    if len(parts) == 5 and parts[4] == "events" and method == "GET":
                        after_id = query.get("after_id", [None])[0]
                        self.respond_list(runtime.store.list_events(session_id, after_id))
                        return
                    if len(parts) == 5 and parts[4] == "events" and method == "POST":
                        payload = self.read_json()
                        event_type = payload.get("type", "user.message")
                        if event_type == "tool.requested":
                            event = runtime.store.request_tool(
                                session_id,
                                payload.get("payload", payload),
                                request_id,
                            )
                        else:
                            event = runtime.store.append_event(
                                session_id,
                                event_type,
                                payload.get("payload", {}),
                                request_id,
                            )
                        if event["type"].startswith("user."):
                            self.respond_json(
                                HTTPStatus.ACCEPTED,
                                runtime.store.enqueue_session_turn(
                                    session_id,
                                    event,
                                    request_id=request_id,
                                    source="api",
                                ),
                            )
                            return
                        self.respond_json(HTTPStatus.CREATED, event)
                        return
                    if len(parts) == 6 and parts[4] == "events" and parts[5] == "stream" and method == "GET":
                        self.respond_sse(session_id, query)
                        return
                    if len(parts) == 5 and parts[4] == "artifacts" and method == "GET":
                        self.respond_list(runtime.store.list_artifacts(session_id))
                        return
                    if len(parts) == 5 and parts[4] == "pending-actions" and method == "GET":
                        self.respond_list(runtime.store.list_pending_actions(session_id))
                        return
                    if (
                        len(parts) == 7
                        and parts[4] == "pending-actions"
                        and parts[6] == "resolve"
                        and method == "POST"
                    ):
                        self.respond_json(
                            HTTPStatus.OK,
                            runtime.store.resolve_pending_action(
                                session_id,
                                parts[5],
                                self.read_json(),
                                request_id,
                            ),
                        )
                        return
                    if len(parts) == 5 and parts[4] == "audit" and method == "GET":
                        self.respond_list(self.audit_for_session(session_id))
                        return
                    if len(parts) == 5 and parts[4] == "usage" and method == "GET":
                        self.respond_list(runtime.store.list_usage(session_id))
                        return

                if method == "GET" and path == "/api/v1/jobs":
                    self.respond_list(runtime.store.list_jobs())
                    return
                if method == "POST" and path == "/api/v1/jobs":
                    self.respond_json(
                        HTTPStatus.CREATED,
                        runtime.store.create_job(self.read_json(), request_id),
                    )
                    return
                if len(parts) == 4 and parts[:3] == ["api", "v1", "jobs"] and method == "GET":
                    self.respond_json(HTTPStatus.OK, runtime.store.get_job(parts[3]))
                    return
                if len(parts) == 5 and parts[:3] == ["api", "v1", "jobs"] and parts[4] == "trigger" and method == "POST":
                    self.respond_json(
                        HTTPStatus.ACCEPTED,
                        runtime.store.trigger_job(parts[3], request_id=request_id),
                    )
                    return
                if len(parts) == 5 and parts[:3] == ["api", "v1", "jobs"] and parts[4] == "enqueue" and method == "POST":
                    self.respond_json(
                        HTTPStatus.ACCEPTED,
                        runtime.store.enqueue_job(parts[3], request_id=request_id),
                    )
                    return

                if method == "GET" and path == "/api/v1/workers":
                    self.respond_list(runtime.store.list_workers())
                    return
                if method == "POST" and path == "/api/v1/workers":
                    self.respond_json(
                        HTTPStatus.CREATED,
                        runtime.store.register_worker(self.read_json(), request_id),
                    )
                    return
                if (
                    len(parts) == 5
                    and parts[:3] == ["api", "v1", "workers"]
                    and parts[4] == "heartbeat"
                    and method == "POST"
                ):
                    self.respond_json(
                        HTTPStatus.OK,
                        runtime.store.heartbeat_worker(parts[3], request_id),
                    )
                    return
                if (
                    len(parts) == 5
                    and parts[:3] == ["api", "v1", "workers"]
                    and parts[4] == "claim"
                    and method == "POST"
                ):
                    payload = self.read_json()
                    self.respond_json(
                        HTTPStatus.OK,
                        runtime.store.claim_next_run(
                            parts[3],
                            request_id,
                            int(payload.get("lease_seconds", 900)),
                        ),
                    )
                    return
                if (
                    len(parts) == 7
                    and parts[:3] == ["api", "v1", "workers"]
                    and parts[4] == "runs"
                    and parts[6] == "execute"
                    and method == "POST"
                ):
                    payload = self.read_json()
                    self.respond_json(
                        HTTPStatus.OK,
                        runtime.store.execute_claimed_run(
                            parts[3],
                            parts[5],
                            payload.get("lease_token"),
                            request_id,
                        ),
                    )
                    return
                if (
                    len(parts) == 8
                    and parts[:3] == ["api", "v1", "workers"]
                    and parts[4] == "runs"
                    and parts[6] == "turn"
                    and parts[7] == "start"
                    and method == "POST"
                ):
                    payload = self.read_json()
                    self.respond_json(
                        HTTPStatus.OK,
                        runtime.store.start_worker_run_turn(
                            parts[3],
                            parts[5],
                            payload.get("lease_token"),
                            request_id,
                        ),
                    )
                    return
                if (
                    len(parts) == 8
                    and parts[:3] == ["api", "v1", "workers"]
                    and parts[4] == "runs"
                    and parts[6] == "lease"
                    and parts[7] == "renew"
                    and method == "POST"
                ):
                    self.respond_json(
                        HTTPStatus.OK,
                        runtime.store.renew_worker_run_lease(parts[3], parts[5], self.read_json(), request_id),
                    )
                    return
                if (
                    len(parts) == 7
                    and parts[:3] == ["api", "v1", "workers"]
                    and parts[4] == "runs"
                    and parts[6] == "events"
                    and method == "POST"
                ):
                    self.respond_json(
                        HTTPStatus.CREATED,
                        runtime.store.append_worker_run_event(parts[3], parts[5], self.read_json(), request_id),
                    )
                    return
                if (
                    len(parts) == 7
                    and parts[:3] == ["api", "v1", "workers"]
                    and parts[4] == "runs"
                    and parts[6] == "artifacts"
                    and method == "POST"
                ):
                    self.respond_json(
                        HTTPStatus.CREATED,
                        runtime.store.create_worker_run_artifact(parts[3], parts[5], self.read_json(), request_id),
                    )
                    return
                if (
                    len(parts) == 7
                    and parts[:3] == ["api", "v1", "workers"]
                    and parts[4] == "runs"
                    and parts[6] == "usage"
                    and method == "POST"
                ):
                    self.respond_json(
                        HTTPStatus.CREATED,
                        runtime.store.record_worker_run_usage(parts[3], parts[5], self.read_json(), request_id),
                    )
                    return
                if (
                    len(parts) == 8
                    and parts[:3] == ["api", "v1", "workers"]
                    and parts[4] == "runs"
                    and parts[6] == "tools"
                    and parts[7] == "execute"
                    and method == "POST"
                ):
                    self.respond_json(
                        HTTPStatus.OK,
                        runtime.store.execute_worker_run_tool(parts[3], parts[5], self.read_json(), request_id),
                    )
                    return
                if (
                    len(parts) == 7
                    and parts[:3] == ["api", "v1", "workers"]
                    and parts[4] == "runs"
                    and parts[6] == "complete"
                    and method == "POST"
                ):
                    self.respond_json(
                        HTTPStatus.OK,
                        runtime.store.complete_worker_run(parts[3], parts[5], self.read_json(), request_id),
                    )
                    return

                if method == "GET" and path == "/api/v1/runs":
                    self.respond_list(runtime.store.list_runs())
                    return
                if len(parts) == 4 and parts[:3] == ["api", "v1", "runs"] and method == "GET":
                    self.respond_json(HTTPStatus.OK, runtime.store.get_run(parts[3]))
                    return

                if method == "GET" and path == "/api/v1/integrations":
                    self.respond_list(runtime.store.list_integrations())
                    return
                if method == "POST" and path == "/api/v1/integrations":
                    self.respond_json(
                        HTTPStatus.CREATED,
                        runtime.store.create_integration(self.read_json(), request_id),
                    )
                    return
                if len(parts) == 4 and parts[:3] == ["api", "v1", "integrations"] and method == "GET":
                    self.respond_json(HTTPStatus.OK, runtime.store.get_integration(parts[3]))
                    return
                if (
                    len(parts) == 5
                    and parts[:3] == ["api", "v1", "integrations"]
                    and parts[4] == "credential"
                    and method == "POST"
                ):
                    self.respond_json(
                        HTTPStatus.OK,
                        runtime.store.register_integration_credential(
                            parts[3], self.read_json(), request_id
                        ),
                    )
                    return

                if method == "GET" and path == "/api/v1/vaults":
                    self.respond_list(runtime.store.list_vaults())
                    return
                if method == "POST" and path == "/api/v1/vaults":
                    self.respond_json(HTTPStatus.CREATED, runtime.store.create_vault(self.read_json(), request_id))
                    return
                if len(parts) == 4 and parts[:3] == ["api", "v1", "vaults"] and method == "GET":
                    self.respond_json(HTTPStatus.OK, runtime.store.get_vault(parts[3]))
                    return
                if (
                    len(parts) == 5
                    and parts[:3] == ["api", "v1", "vaults"]
                    and parts[4] == "credentials"
                    and method == "GET"
                ):
                    self.respond_list(runtime.store.list_vault_credentials(parts[3]))
                    return
                if (
                    len(parts) == 5
                    and parts[:3] == ["api", "v1", "vaults"]
                    and parts[4] == "credentials"
                    and method == "POST"
                ):
                    self.respond_json(
                        HTTPStatus.CREATED,
                        runtime.store.create_vault_credential(parts[3], self.read_json(), request_id),
                    )
                    return

                if method == "GET" and path == "/api/v1/files":
                    self.respond_list(runtime.store.list_files())
                    return
                if method == "POST" and path == "/api/v1/files":
                    self.respond_json(HTTPStatus.CREATED, runtime.store.create_file(self.read_json(), request_id))
                    return
                if len(parts) == 4 and parts[:3] == ["api", "v1", "files"] and method == "GET":
                    self.respond_json(HTTPStatus.OK, runtime.store.get_file(parts[3]))
                    return
                if len(parts) == 5 and parts[:3] == ["api", "v1", "files"] and parts[4] == "content" and method == "GET":
                    content, content_type = runtime.store.get_file_content(parts[3])
                    self.respond_bytes(HTTPStatus.OK, content, content_type)
                    return

                if method == "GET" and path == "/api/v1/artifacts":
                    self.respond_list(runtime.store.list_artifacts())
                    return
                if len(parts) == 4 and parts[:3] == ["api", "v1", "artifacts"] and method == "GET":
                    self.respond_json(HTTPStatus.OK, runtime.store.get_artifact(parts[3]))
                    return
                if (
                    len(parts) == 5
                    and parts[:3] == ["api", "v1", "artifacts"]
                    and parts[4] == "content"
                    and method == "GET"
                ):
                    content, content_type = runtime.store.get_artifact_content(parts[3])
                    self.respond_bytes(HTTPStatus.OK, content, content_type)
                    return

                if method == "GET" and path == "/api/v1/tools":
                    self.respond_list(runtime.store.list_tools())
                    return
                if method == "GET" and path == "/api/v1/tool-policies":
                    self.respond_list(runtime.store.list_tool_policies())
                    return
                if method == "POST" and path == "/api/v1/tool-policies":
                    self.respond_json(
                        HTTPStatus.CREATED,
                        runtime.store.create_tool_policy(self.read_json(), request_id),
                    )
                    return
                if len(parts) == 4 and parts[:3] == ["api", "v1", "tool-policies"] and method == "GET":
                    self.respond_json(HTTPStatus.OK, runtime.store.get_tool_policy(parts[3]))
                    return

                if is_webhook_trigger:
                    webhook_token = self.headers.get("X-CloudAgent-Webhook-Token")
                    self.respond_json(
                        HTTPStatus.ACCEPTED,
                        runtime.store.trigger_integration_webhook(
                            parts[3],
                            parts[4],
                            self.read_json(),
                            request_id,
                            webhook_token,
                        ),
                    )
                    return

                if method == "GET" and path == "/api/v1/admin/overview":
                    self.respond_json(HTTPStatus.OK, runtime.store.overview())
                    return
                if method == "POST" and path == "/api/v1/admin/showcase/bootstrap":
                    self.respond_json(
                        HTTPStatus.OK,
                        runtime.store.bootstrap_showcase(request_id),
                    )
                    return

                self.respond_error(HTTPStatus.NOT_FOUND, "not_found_error", f"No route for {path}", request_id)
            except PermissionError as exc:
                self.respond_error(HTTPStatus.UNAUTHORIZED, "authentication_error", str(exc), request_id)
            except PayloadTooLargeError as exc:
                self.respond_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "payload_too_large_error", str(exc), request_id)
            except KeyError as exc:
                self.respond_error(HTTPStatus.NOT_FOUND, "not_found_error", str(exc), request_id)
            except ValueError as exc:
                self.respond_error(HTTPStatus.BAD_REQUEST, "invalid_request_error", str(exc), request_id)

        def require_auth(self) -> None:
            auth = self.headers.get("Authorization", "")
            if not hmac.compare_digest(auth, f"Bearer {runtime.auth_token}"):
                raise PermissionError("missing or invalid bearer token")

        def read_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("invalid Content-Length") from exc
            if length > runtime.max_json_bytes:
                raise PayloadTooLargeError("JSON body is too large")
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError("invalid JSON body") from exc
            if not isinstance(value, dict):
                raise ValueError("JSON body must be an object")
            return value

        def audit_for_session(self, session_id: str) -> list[dict[str, Any]]:
            rows = runtime.store.fetch_all(
                """
                SELECT * FROM audit_log
                WHERE target_id = ? OR id IN (
                    SELECT audit_ref FROM events WHERE session_id = ? AND audit_ref IS NOT NULL
                )
                ORDER BY created_at DESC
                """,
                (session_id, session_id),
            )
            return [dict(row) for row in rows]

        def respond_sse(self, session_id: str, query: dict[str, list[str]]) -> None:
            after_id = query.get("after_id", [None])[0] or self.headers.get("Last-Event-ID")
            once = query.get("once", ["0"])[0] == "1"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_common_headers()
            self.end_headers()
            last_id = after_id
            deadline = time.monotonic() + (0 if once else 30)
            while True:
                events = runtime.store.list_events(session_id, last_id)
                for event in events:
                    last_id = event["id"]
                    self.wfile.write(f"id: {event['id']}\n".encode("utf-8"))
                    self.wfile.write(f"event: {event['type']}\n".encode("utf-8"))
                    self.wfile.write(f"data: {json_dumps(event)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                if once or time.monotonic() >= deadline:
                    return
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                time.sleep(2)

        def respond_list(self, data: list[dict[str, Any]]) -> None:
            self.respond_json(
                HTTPStatus.OK,
                {
                    "data": data,
                    "first_id": data[0]["id"] if data else None,
                    "last_id": data[-1]["id"] if data else None,
                    "has_more": False,
                },
            )

        def respond_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json_dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_common_headers()
            self.end_headers()
            self.wfile.write(body)

        def respond_html(self, status: HTTPStatus, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_common_headers()
            self.end_headers()
            self.wfile.write(body)

        def respond_console_asset(self, status: HTTPStatus, asset: Any) -> None:
            self.send_response(status)
            self.send_header("Content-Type", asset.content_type)
            self.send_header("Content-Length", str(len(asset.content)))
            self.send_header("Cache-Control", asset.cache_control)
            self.send_common_headers()
            self.end_headers()
            self.wfile.write(asset.content)

        def respond_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", "attachment")
            self.send_header("Cache-Control", "no-store")
            self.send_common_headers()
            self.end_headers()
            self.wfile.write(body)

        def respond_error(
            self,
            status: HTTPStatus,
            error_type: str,
            message: str,
            request_id: str,
        ) -> None:
            self.respond_json(
                status,
                {
                    "type": "error",
                    "error": {
                        "type": error_type,
                        "message": message,
                        "request_id": request_id,
                    },
                },
            )

        def send_common_headers(self) -> None:
            origin = self.headers.get("Origin")
            allowed_origin = self.allowed_origin(origin)
            if allowed_origin:
                self.send_header("Access-Control-Allow-Origin", allowed_origin)
                self.send_header("Vary", "Origin")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Authorization, Content-Type, X-Request-Id, X-CloudAgent-Webhook-Token",
            )
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Permissions-Policy", "camera=(), geolocation=(), microphone=(), payment=(), usb=()")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; "
                "object-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'",
            )

        def allowed_origin(self, origin: str | None) -> str | None:
            if not origin:
                return None
            host = self.headers.get("Host")
            same_origin = {f"http://{host}", f"https://{host}"} if host else set()
            if origin in same_origin or origin in runtime.cors_origins:
                return origin
            return None

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}", flush=True)

    return Handler
