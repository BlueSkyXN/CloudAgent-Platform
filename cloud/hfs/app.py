from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


PORT = int(os.environ.get("PORT", "7860"))
STARTED_AT = datetime.now(timezone.utc).isoformat()
VERSION = os.environ.get("CLOUDAGENT_VERSION", "0.1.0-sdlc-probe")
SDLC_STATUS = os.environ.get("CLOUDAGENT_SDLC_STATUS", "review-ready-draft")
HFS_MODE = os.environ.get("CLOUDAGENT_HFS_MODE", "deployment-probe")
FULL_RUNTIME_IMPLEMENTED = (
    os.environ.get("CLOUDAGENT_FULL_RUNTIME_IMPLEMENTED", "false").lower()
    in {"1", "true", "yes"}
)
BUNDLE_MANIFEST_PATH = Path(
    os.environ.get("CLOUDAGENT_BUNDLE_MANIFEST", "/app/BUNDLE_MANIFEST.json")
)
BUILD_SOURCE_PATH = Path(os.environ.get("CLOUDAGENT_BUILD_SOURCE", "/app/BUILD_SOURCE.txt"))


def json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def read_key_value_file(path: Path) -> dict[str, str] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    values: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def runtime_capabilities() -> dict[str, Any]:
    return {
        "deployment_probe": HFS_MODE == "deployment-probe",
        "full_runtime_implemented": FULL_RUNTIME_IMPLEMENTED,
        "kernel_adapter_runtime": FULL_RUNTIME_IMPLEMENTED,
        "sandbox_matrix_execution": FULL_RUNTIME_IMPLEMENTED,
        "event_store": FULL_RUNTIME_IMPLEMENTED,
        "tool_gateway": FULL_RUNTIME_IMPLEMENTED,
        "vault": FULL_RUNTIME_IMPLEMENTED,
        "artifact_store": FULL_RUNTIME_IMPLEMENTED,
        "audit_usage_metering": FULL_RUNTIME_IMPLEMENTED,
    }


def readiness_payload() -> tuple[HTTPStatus, dict[str, Any]]:
    dependencies: list[dict[str, Any]] = [
        {"name": "http_server", "status": "ok", "required": True},
        {
            "name": "bundle_manifest",
            "status": "ok" if BUNDLE_MANIFEST_PATH.exists() else "missing",
            "required": False,
        },
    ]

    if HFS_MODE == "full-runtime":
        dependencies.append(
            {
                "name": "full_runtime_implementation",
                "status": "ok" if FULL_RUNTIME_IMPLEMENTED else "missing",
                "required": True,
            }
        )
        for name, env_key in {
            "event_store": "CLOUDAGENT_EVENT_STORE_URL",
            "execution_endpoint": "CLOUDAGENT_EXECUTION_ENDPOINT",
            "sandbox_policy": "CLOUDAGENT_SANDBOX_POLICY",
        }.items():
            dependencies.append(
                {
                    "name": name,
                    "status": "ok" if os.environ.get(env_key) else "missing",
                    "required": True,
                    "env": env_key,
                }
            )

    ready = all(
        item["status"] == "ok" for item in dependencies if item.get("required")
    )
    payload = {
        "status": "ready" if ready else "not_ready",
        "service": "CloudAgent-Platform",
        "mode": HFS_MODE,
        "sdlc_status": SDLC_STATUS,
        "full_runtime_implemented": FULL_RUNTIME_IMPLEMENTED,
        "dependencies": dependencies,
        "runtime_capabilities": runtime_capabilities(),
    }
    return (HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE, payload)


def openapi() -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "CloudAgent-Platform HFS Deployment Probe",
            "version": VERSION,
            "description": "Minimal HFS runtime probe for the 20260616 SDLC package.",
        },
        "paths": {
            "/_ops/healthz": {
                "get": {
                    "summary": "Health check",
                    "responses": {
                        "200": {
                            "description": "healthy",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Health"}
                                }
                            },
                        }
                    },
                }
            },
            "/_ops/readyz": {
                "get": {
                    "summary": "Readiness check",
                    "responses": {
                        "200": {
                            "description": "ready",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Readiness"}
                                }
                            },
                        },
                        "503": {
                            "description": "not ready",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Readiness"}
                                }
                            },
                        },
                    },
                }
            },
            "/api/v1/system/info": {
                "get": {
                    "summary": "Deployment metadata",
                    "responses": {
                        "200": {
                            "description": "system info",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/SystemInfo"}
                                }
                            },
                        }
                    },
                }
            },
            "/api/v1/sdlc/status": {
                "get": {
                    "summary": "SDLC package status",
                    "responses": {
                        "200": {
                            "description": "status",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/SdlcStatus"}
                                }
                            },
                        }
                    },
                }
            },
            "/openapi.json": {
                "get": {
                    "summary": "OpenAPI document",
                    "responses": {
                        "200": {
                            "description": "OpenAPI 3.1 document",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        }
                    },
                }
            },
        },
        "components": {
            "schemas": {
                "Health": {
                    "type": "object",
                    "required": ["status", "service", "mode", "started_at"],
                    "properties": {
                        "status": {"type": "string"},
                        "service": {"type": "string"},
                        "mode": {"type": "string"},
                        "started_at": {"type": "string", "format": "date-time"},
                    },
                },
                "Readiness": {
                    "type": "object",
                    "required": ["status", "service", "dependencies"],
                    "properties": {
                        "status": {"type": "string", "enum": ["ready", "not_ready"]},
                        "service": {"type": "string"},
                        "dependencies": {"type": "array", "items": {"type": "object"}},
                        "runtime_capabilities": {"type": "object"},
                    },
                },
                "SystemInfo": {"type": "object"},
                "SdlcStatus": {"type": "object"},
                "Error": {"type": "object"},
            }
        },
    }


def system_info() -> dict[str, Any]:
    bundle_manifest = read_json_file(BUNDLE_MANIFEST_PATH)
    build_source = read_key_value_file(BUILD_SOURCE_PATH)
    return {
        "type": "cloudagent.hfs.system_info",
        "service": "CloudAgent-Platform",
        "version": VERSION,
        "mode": HFS_MODE,
        "runtime": "huggingface-space-docker",
        "port": PORT,
        "started_at": STARTED_AT,
        "health_endpoint": "/_ops/healthz",
        "readiness_endpoint": "/_ops/readyz",
        "sdlc_package": "local/20260616",
        "full_runtime_implemented": FULL_RUNTIME_IMPLEMENTED,
        "runtime_capabilities": runtime_capabilities(),
        "bundle_manifest": bundle_manifest,
        "build_source": build_source,
    }


def sdlc_status() -> dict[str, Any]:
    return {
        "type": "cloudagent.sdlc.status",
        "status": SDLC_STATUS,
        "source_packages": [
            "local/20260615-A",
            "local/20260615-B",
            "local/20260615-C",
            "local/20260615-D",
        ],
        "generated_package": "local/20260616",
        "product_definition": (
            "multi-kernel API-first cloud Agent Runtime Platform with "
            "Kernel Adapter Layer and Sandbox Matrix"
        ),
        "hfs_support": {
            "space_sdk": "docker",
            "health": "/_ops/healthz",
            "readiness": "/_ops/readyz",
            "verification_mode": "real-hfs-live-smoke",
        },
        "implemented_scope": "deployment-probe",
        "runtime_capabilities": runtime_capabilities(),
        "not_implemented_yet": [
            "kernel-adapter-runtime",
            "sandbox-matrix-execution",
            "event-store",
            "tool-gateway",
            "vault",
            "artifact-store",
            "audit-usage-metering",
        ],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "CloudAgentHFSProbe/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")

        if path in {"/", "/index.html"}:
            self.respond_text(
                HTTPStatus.OK,
                "CloudAgent-Platform HFS deployment probe is running.\n"
                "Use /_ops/healthz, /_ops/readyz, /api/v1/system/info, "
                "/api/v1/sdlc/status, or /openapi.json.\n",
            )
            return

        if path == "/_ops/readyz":
            status, payload = readiness_payload()
            self.respond_json(status, payload)
            return

        routes = {
            "/_ops/healthz": {
                "status": "ok",
                "service": "CloudAgent-Platform",
                "mode": HFS_MODE,
                "started_at": STARTED_AT,
            },
            "/api/v1/system/info": system_info(),
            "/api/v1/sdlc/status": sdlc_status(),
            "/openapi.json": openapi(),
        }

        payload = routes.get(path)
        if payload is None:
            self.respond_json(
                HTTPStatus.NOT_FOUND,
                {
                    "type": "error",
                    "error": {
                        "type": "not_found_error",
                        "message": f"No route for {path}",
                    },
                },
            )
            return

        self.respond_json(HTTPStatus.OK, payload)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def respond_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_text(self, status: HTTPStatus, body_text: str) -> None:
        body = body_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"CloudAgent-Platform HFS deployment probe listening on :{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
