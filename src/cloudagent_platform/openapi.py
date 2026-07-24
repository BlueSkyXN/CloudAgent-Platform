from __future__ import annotations

import re

from . import __version__


_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
_PATH_PARAMETER = re.compile(r"\{([^}]+)\}")


def _operation_id(method: str, path: str) -> str:
    """Return a stable, unique operation id derived from the implemented route."""
    parts = [part.strip("{}") for part in path.split("/") if part]
    return "_".join([method.lower(), *parts]).replace("-", "_")


def _success_statuses(method: str, path: str) -> tuple[str, ...]:
    if method != "post":
        return ("200",)
    if path == "/api/v1/sessions/{session_id}/events":
        # A normal event is persisted immediately, while user.message is
        # accepted after it has queued worker execution.
        return ("201", "202")
    if path in {
        "/api/v1/agents",
        "/api/v1/environments",
        "/api/v1/sessions",
        "/api/v1/jobs",
        "/api/v1/workers",
        "/api/v1/integrations",
        "/api/v1/vaults",
        "/api/v1/files",
        "/api/v1/tool-policies",
    } or path.endswith("/credentials") or path.endswith("/events") or path.endswith("/artifacts") or path.endswith("/usage"):
        return ("201",)
    if path.endswith("/trigger") or path.endswith("/enqueue") or "/webhooks/" in path:
        return ("202",)
    return ("200",)


def _complete_contract(paths: dict[str, dict[str, object]]) -> None:
    """Apply route-level contract invariants to the generated implementation map.

    Keeping this normalization beside the route inventory makes a newly added
    handler fail closed in the contract test instead of silently shipping an
    undocumented path parameter, response, or operation id.
    """
    for path, path_item in paths.items():
        parameter_names = _PATH_PARAMETER.findall(path)
        if parameter_names:
            path_item["parameters"] = [
                {
                    "name": name,
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                }
                for name in parameter_names
            ]
        for method, value in path_item.items():
            if method not in _HTTP_METHODS or not isinstance(value, dict):
                continue
            value.setdefault("operationId", _operation_id(method, path))
            responses = value.setdefault("responses", {})
            if not isinstance(responses, dict):
                raise ValueError(f"OpenAPI responses must be an object for {method.upper()} {path}")
            for status in _success_statuses(method, path):
                responses.setdefault(
                    status,
                    {
                        "description": "Successful response",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    },
                )
            if path.endswith("/events/stream"):
                responses["200"] = {
                    "description": "Server-sent session event stream",
                    "content": {"text/event-stream": {"schema": {"type": "string"}}},
                }
            responses.setdefault("400", {"$ref": "#/components/responses/InvalidRequest"})
            if value.get("security", [{"BearerAuth": []}]):
                responses.setdefault("401", {"$ref": "#/components/responses/AuthenticationError"})
            responses.setdefault("404", {"$ref": "#/components/responses/NotFound"})


def current_openapi() -> dict[str, object]:
    public_json_response = {
        "200": {
            "description": "Public operational response",
            "content": {"application/json": {"schema": {"type": "object"}}},
        }
    }
    paths = {
        "/_ops/healthz": {
            "get": {"summary": "Health check", "security": [], "responses": public_json_response}
        },
        "/_ops/readyz": {
            "get": {"summary": "Readiness check", "security": [], "responses": public_json_response}
        },
        "/openapi.json": {
            "get": {
                "summary": "Current implemented OpenAPI document",
                "security": [],
                "responses": public_json_response,
            }
        },
        "/api/v1/sdlc/status": {
            "get": {"summary": "Local SDLC/runtime status", "security": [], "responses": public_json_response}
        },
        "/api/v1/system/info": {"get": {"summary": "System information"}},
        "/api/v1/permission-profiles": {"get": {"summary": "List permission profiles"}},
        "/api/v1/permission-profiles/{profile_id}": {"get": {"summary": "Get permission profile"}},
        "/api/v1/sandbox-profiles": {"get": {"summary": "List sandbox profiles"}},
        "/api/v1/sandbox-profiles/{profile_id}": {"get": {"summary": "Get sandbox profile"}},
        "/api/v1/kernels": {"get": {"summary": "List kernel providers"}},
        "/api/v1/kernels/{kernel_id}": {"get": {"summary": "Get kernel provider"}},
        "/api/v1/kernels/{kernel_id}/probe": {"post": {"summary": "Run safe kernel probe"}},
        "/api/v1/agents": {"get": {"summary": "List agents"}, "post": {"summary": "Create agent"}},
        "/api/v1/environments": {
            "get": {"summary": "List environments"},
            "post": {"summary": "Create environment"},
        },
        "/api/v1/sessions": {"get": {"summary": "List sessions"}, "post": {"summary": "Create session"}},
        "/api/v1/sessions/{session_id}": {"get": {"summary": "Get session"}},
        "/api/v1/sessions/{session_id}/events": {
            "get": {"summary": "List session events"},
            "post": {"summary": "Append event; user events queue worker-side turns"},
        },
        "/api/v1/sessions/{session_id}/events/stream": {"get": {"summary": "Stream session events"}},
        "/api/v1/sessions/{session_id}/artifacts": {"get": {"summary": "List session artifacts"}},
        "/api/v1/sessions/{session_id}/pending-actions": {"get": {"summary": "List pending actions"}},
        "/api/v1/sessions/{session_id}/pending-actions/{action_id}/resolve": {
            "post": {"summary": "Resolve pending action"}
        },
        "/api/v1/sessions/{session_id}/audit": {"get": {"summary": "List session audit records"}},
        "/api/v1/sessions/{session_id}/usage": {"get": {"summary": "List session usage"}},
        "/api/v1/jobs": {"get": {"summary": "List jobs"}, "post": {"summary": "Create job"}},
        "/api/v1/jobs/{job_id}": {"get": {"summary": "Get job"}},
        "/api/v1/jobs/{job_id}/trigger": {"post": {"summary": "Trigger job asynchronously"}},
        "/api/v1/jobs/{job_id}/enqueue": {"post": {"summary": "Enqueue job for worker execution"}},
        "/api/v1/runs": {"get": {"summary": "List runs"}},
        "/api/v1/runs/{run_id}": {"get": {"summary": "Get run"}},
        "/api/v1/workers": {"get": {"summary": "List workers"}, "post": {"summary": "Register worker"}},
        "/api/v1/workers/{worker_id}/heartbeat": {"post": {"summary": "Worker heartbeat"}},
        "/api/v1/workers/{worker_id}/claim": {"post": {"summary": "Claim next queued run"}},
        "/api/v1/workers/{worker_id}/runs/{run_id}/execute": {
            "post": {"summary": "Legacy server-side execution of a claimed run with lease_token"}
        },
        "/api/v1/workers/{worker_id}/runs/{run_id}/turn/start": {
            "post": {"summary": "Start a worker-side turn for a claimed run with lease_token"}
        },
        "/api/v1/workers/{worker_id}/runs/{run_id}/lease/renew": {
            "post": {"summary": "Renew a worker-side run lease with lease_token"}
        },
        "/api/v1/workers/{worker_id}/runs/{run_id}/events": {
            "post": {"summary": "Append a worker-side run event with lease_token"}
        },
        "/api/v1/workers/{worker_id}/runs/{run_id}/artifacts": {
            "post": {"summary": "Create a worker-side run artifact with lease_token"}
        },
        "/api/v1/workers/{worker_id}/runs/{run_id}/usage": {
            "post": {"summary": "Record worker-side run usage with lease_token"}
        },
        "/api/v1/workers/{worker_id}/runs/{run_id}/tools/execute": {
            "post": {"summary": "Execute an approved tool action with lease_token"}
        },
        "/api/v1/workers/{worker_id}/runs/{run_id}/complete": {
            "post": {"summary": "Complete a worker-side run with lease_token"}
        },
        "/api/v1/integrations": {
            "get": {"summary": "List integrations"},
            "post": {"summary": "Create integration"},
        },
        "/api/v1/integrations/{integration_id}": {"get": {"summary": "Get integration"}},
        "/api/v1/integrations/{integration_id}/credential": {
            "post": {
                "summary": "Register or replace a process-local integration credential",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/IntegrationCredentialRequest"}
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Redacted integration with credential status configured",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Integration"}
                            }
                        },
                    },
                    "400": {"$ref": "#/components/responses/InvalidRequest"},
                    "401": {"$ref": "#/components/responses/AuthenticationError"},
                    "404": {"$ref": "#/components/responses/NotFound"},
                },
            }
        },
        "/api/v1/vaults": {"get": {"summary": "List vaults"}, "post": {"summary": "Create vault"}},
        "/api/v1/vaults/{vault_id}": {"get": {"summary": "Get vault"}},
        "/api/v1/vaults/{vault_id}/credentials": {
            "get": {"summary": "List vault credentials"},
            "post": {
                "summary": "Create write-only vault credential reference",
                "responses": {
                    "201": {
                        "description": "Redacted vault credential",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/VaultCredential"}
                            }
                        },
                    }
                },
            },
        },
        "/api/v1/webhooks/{provider}/{integration_id}": {
            "post": {
                "summary": "Accept signed webhook trigger asynchronously",
                "security": [{"WebhookToken": []}],
                "responses": {
                    "202": {"description": "Webhook accepted"},
                    "400": {"$ref": "#/components/responses/InvalidRequest"},
                    "401": {"$ref": "#/components/responses/AuthenticationError"},
                },
            }
        },
        "/api/v1/files": {"get": {"summary": "List files"}, "post": {"summary": "Create JSON-backed file"}},
        "/api/v1/files/{file_id}": {"get": {"summary": "Get file metadata"}},
        "/api/v1/files/{file_id}/content": {
            "get": {
                "summary": "Download file content",
                "responses": {
                    "200": {
                        "description": "Raw file bytes",
                        "content": {
                            "application/octet-stream": {
                                "schema": {"type": "string", "format": "binary"}
                            }
                        },
                    },
                    "404": {"$ref": "#/components/responses/NotFound"},
                },
            }
        },
        "/api/v1/artifacts": {
            "get": {
                "summary": "List artifacts across sessions",
                "responses": {
                    "200": {
                        "description": "Artifact list",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ListResponse"}
                            }
                        },
                    }
                },
            }
        },
        "/api/v1/artifacts/{artifact_id}": {
            "get": {
                "summary": "Get artifact metadata",
                "responses": {
                    "200": {
                        "description": "Artifact metadata",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Artifact"}
                            }
                        },
                    },
                    "404": {"$ref": "#/components/responses/NotFound"},
                },
            }
        },
        "/api/v1/artifacts/{artifact_id}/content": {
            "get": {
                "summary": "Download artifact content",
                "responses": {
                    "200": {
                        "description": "Raw artifact bytes",
                        "content": {
                            "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                        },
                    },
                    "404": {"$ref": "#/components/responses/NotFound"},
                },
            }
        },
        "/api/v1/tools": {"get": {"summary": "List built-in tools"}},
        "/api/v1/tool-policies": {
            "get": {"summary": "List tool policies"},
            "post": {"summary": "Create tool policy"},
        },
        "/api/v1/tool-policies/{policy_id}": {"get": {"summary": "Get tool policy"}},
        "/api/v1/admin/overview": {"get": {"summary": "Admin overview"}},
        "/api/v1/admin/sessions/{session_id}/workspace": {
            "get": {
                "summary": "Get the complete Console session workspace read model",
                "responses": {
                    "200": {
                        "description": "Session, timeline, approvals, evidence, usage, audit, and tools",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SessionWorkspace"}
                            }
                        },
                    }
                },
            }
        },
        "/api/v1/admin/showcase/bootstrap": {
            "post": {
                "summary": "Idempotently create local showcase resources",
                "responses": {
                    "200": {
                        "description": "Local showcase resources and whether each was created or reused.",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ShowcaseBootstrapResponse"}
                            }
                        },
                    }
                },
            }
        },
    }
    _complete_contract(paths)
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "CloudAgent-Platform Local Prototype",
            "version": __version__,
            "description": "Current implemented local prototype contract. The broader SDLC draft remains in local/20260616/22-openapi-draft.yaml.",
        },
        "servers": [{"url": "http://127.0.0.1:8080"}],
        "security": [{"BearerAuth": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "BearerAuth": {"type": "http", "scheme": "bearer"},
                "WebhookToken": {"type": "apiKey", "in": "header", "name": "X-CloudAgent-Webhook-Token"},
            },
            "responses": {
                "InvalidRequest": {
                    "description": "Invalid request",
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Error"}}
                    },
                },
                "AuthenticationError": {
                    "description": "Authentication or webhook token failure",
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Error"}}
                    },
                },
                "NotFound": {
                    "description": "Resource not found",
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Error"}}
                    },
                },
            },
            "schemas": {
                "Error": {
                    "type": "object",
                    "required": ["type", "error"],
                    "properties": {
                        "type": {"type": "string", "const": "error"},
                        "error": {
                            "type": "object",
                            "required": ["type", "message", "request_id"],
                            "properties": {
                                "type": {"type": "string"},
                                "message": {"type": "string"},
                                "request_id": {"type": "string"},
                            },
                        },
                    },
                },
                "ListResponse": {
                    "type": "object",
                    "required": ["data", "first_id", "last_id", "has_more"],
                    "properties": {
                        "data": {"type": "array", "items": {"type": "object"}},
                        "first_id": {"type": ["string", "null"]},
                        "last_id": {"type": ["string", "null"]},
                        "has_more": {"type": "boolean"},
                    },
                },
                "KernelProvider": {"type": "object", "required": ["id", "type", "capabilities", "status"]},
                "PermissionProfile": {
                    "type": "object",
                    "required": ["id", "type", "status", "tool_policy_defaults"],
                    "properties": {
                        "id": {"type": "string"},
                        "type": {"type": "string", "const": "permission_profile"},
                        "status": {"type": "string"},
                        "tool_policy_defaults": {"type": "object"},
                    },
                },
                "SandboxProfile": {
                    "type": "object",
                    "required": ["id", "type", "status", "provider", "isolation"],
                    "properties": {
                        "id": {"type": "string"},
                        "type": {"type": "string", "const": "sandbox_profile"},
                        "status": {"type": "string"},
                        "provider": {"type": "string"},
                        "isolation": {"type": "object"},
                    },
                },
                "Agent": {"type": "object", "required": ["id", "type", "name", "kernel", "version", "status"]},
                "Environment": {"type": "object", "required": ["id", "type", "name", "runtime_type", "status"]},
                "Session": {"type": "object", "required": ["id", "type", "status", "turn_status", "last_event_id"]},
                "Event": {"type": "object", "required": ["id", "sequence", "session_id", "type", "payload", "created_at"]},
                "Job": {"type": "object", "required": ["id", "type", "name", "trigger", "status"]},
                "JobRun": {
                    "type": "object",
                    "required": ["id", "type", "session_id", "trigger_source", "status", "lease_generation"],
                    "properties": {"lease_token": {"type": "string", "description": "Only returned to the current claiming worker."}},
                },
                "Worker": {"type": "object", "required": ["id", "type", "name", "status", "capabilities", "active_run_id"]},
                "Integration": {
                    "type": "object",
                    "required": [
                        "id", "type", "provider", "name", "status", "credential_status", "capabilities"
                    ],
                    "properties": {
                        "secret_ref": {"type": ["string", "null"], "description": "Digest reference only; raw secret values are never returned or persisted."},
                        "credential_status": {
                            "type": "string",
                            "enum": ["registered", "registration_required"],
                            "description": "Registered means this process currently holds a write-only credential in memory."
                        },
                    },
                },
                "IntegrationCredentialRequest": {
                    "type": "object",
                    "required": ["secret"],
                    "properties": {
                        "secret": {
                            "type": "string",
                            "writeOnly": True,
                            "description": "Stored only in the running process; never returned or persisted in SQLite."
                        }
                    },
                },
                "Vault": {
                    "type": "object",
                    "required": ["id", "type", "display_name", "credentials", "status"],
                    "properties": {
                        "id": {"type": "string"},
                        "type": {"type": "string", "const": "vault"},
                        "display_name": {"type": "string"},
                        "credentials": {"type": "array", "items": {"$ref": "#/components/schemas/VaultCredential"}},
                    },
                },
                "VaultCredential": {
                    "type": "object",
                    "required": ["id", "type", "vault_id", "auth", "status"],
                    "properties": {
                        "id": {"type": "string"},
                        "type": {"type": "string", "const": "vault_credential"},
                        "vault_id": {"type": "string"},
                        "auth": {
                            "type": "object",
                            "description": "Redacted auth metadata. The prototype retains only a digest reference and does not provide runtime secret injection.",
                        },
                    },
                },
                "Tool": {
                    "type": "object",
                    "required": [
                        "id",
                        "name",
                        "source",
                        "status",
                        "executable",
                        "default_policy",
                        "schema",
                    ],
                },
                "ToolPolicy": {"type": "object", "required": ["id", "scope", "mode"]},
                "PendingAction": {"type": "object", "required": ["id", "session_id", "tool", "proposed_args", "status"]},
                "Artifact": {"type": "object", "required": ["id", "type", "session_id", "name", "content_ref"]},
                "UsageRecord": {"type": "object", "required": ["id", "session_id", "token_input", "token_output"]},
                "WorkerLeaseRequest": {
                    "type": "object",
                    "required": ["lease_token"],
                    "properties": {"lease_token": {"type": "string"}},
                },
                "WorkerCompleteRequest": {
                    "type": "object",
                    "required": ["lease_token", "status", "result"],
                    "properties": {
                        "lease_token": {"type": "string"},
                        "status": {"type": "string", "enum": ["succeeded", "failed", "canceled"]},
                        "result": {"type": "object"},
                    },
                },
                "SessionWorkspace": {
                    "type": "object",
                    "required": [
                        "type",
                        "session",
                        "events",
                        "artifacts",
                        "usage",
                        "pending_actions",
                        "audit",
                        "tools",
                        "counts",
                        "last_event_id",
                    ],
                    "properties": {
                        "type": {
                            "type": "string",
                            "const": "cloudagent.admin.session_workspace",
                        },
                        "session": {"$ref": "#/components/schemas/Session"},
                        "events": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Event"},
                        },
                        "artifacts": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Artifact"},
                        },
                        "usage": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/UsageRecord"},
                        },
                        "pending_actions": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/PendingAction"},
                        },
                        "audit": {"type": "array", "items": {"type": "object"}},
                        "tools": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Tool"},
                        },
                        "counts": {"type": "object"},
                        "last_event_id": {"type": ["string", "null"]},
                    },
                },
                "ShowcaseBootstrapResponse": {
                    "type": "object",
                    "required": [
                        "type",
                        "agent",
                        "environment",
                        "session",
                        "job",
                        "worker",
                        "run",
                        "created",
                        "reused",
                    ],
                    "properties": {
                        "type": {"type": "string", "const": "cloudagent.admin.showcase_bootstrap"},
                        "agent": {"$ref": "#/components/schemas/Agent"},
                        "environment": {"$ref": "#/components/schemas/Environment"},
                        "session": {"$ref": "#/components/schemas/Session"},
                        "job": {"$ref": "#/components/schemas/Job"},
                        "worker": {"$ref": "#/components/schemas/Worker"},
                        "run": {"$ref": "#/components/schemas/JobRun"},
                        "created": {"type": "array", "items": {"type": "string"}},
                        "reused": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
    }
