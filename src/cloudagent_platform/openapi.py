from __future__ import annotations

from . import __version__


def current_openapi() -> dict[str, object]:
    paths = {
        "/_ops/healthz": {"get": {"summary": "Health check"}},
        "/_ops/readyz": {"get": {"summary": "Readiness check"}},
        "/openapi.json": {"get": {"summary": "Current implemented OpenAPI document"}},
        "/api/v1/sdlc/status": {"get": {"summary": "Local SDLC/runtime status"}},
        "/api/v1/system/info": {"get": {"summary": "System information"}},
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
        "/api/v1/webhooks/{provider}/{integration_id}": {
            "post": {"summary": "Accept signed webhook trigger asynchronously"}
        },
        "/api/v1/files": {"get": {"summary": "List files"}, "post": {"summary": "Create JSON-backed file"}},
        "/api/v1/files/{file_id}": {"get": {"summary": "Get file metadata"}},
        "/api/v1/files/{file_id}/content": {"get": {"summary": "Download file content"}},
        "/api/v1/tools": {"get": {"summary": "List built-in tools"}},
        "/api/v1/tool-policies": {"post": {"summary": "Create tool policy"}},
        "/api/v1/tool-policies/{policy_id}": {"get": {"summary": "Get tool policy"}},
        "/api/v1/admin/overview": {"get": {"summary": "Admin overview"}},
    }
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
                    "required": ["id", "type", "provider", "name", "status", "capabilities"],
                    "properties": {
                        "secret_ref": {"type": ["string", "null"], "description": "Digest reference only; raw secret values are never returned."}
                    },
                },
                "Tool": {"type": "object", "required": ["id", "name", "source", "default_policy", "schema"]},
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
            },
        },
    }
