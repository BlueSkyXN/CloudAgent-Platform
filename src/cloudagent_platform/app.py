from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import socket
import ssl
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from .config import (
    DEFAULT_MAX_JSON_BYTES,
    DEFAULT_MAX_STORED_CONTENT_BYTES,
    DEFAULT_PROJECT_ID,
    DEFAULT_RETENTION,
    DEFAULT_TENANT_ID,
    DEFAULT_TOKEN,
    SCHEMA_VERSION,
)
from .connectors import integration_capabilities
from .errors import ConnectorRequestError, PayloadTooLargeError
from .openapi import current_openapi
from .permissions import (
    BASE_TOOL_POLICY_DEFAULTS,
    environment_policy_defaults,
    get_permission_profile,
    get_sandbox_profile,
    list_permission_profiles,
    list_sandbox_profiles,
    merge_tool_policy_defaults,
    more_restrictive_policy_mode,
    policy_mode_to_decision,
    validate_environment_policies,
    validate_profile_for_environment,
    validate_sandbox_for_environment,
)
from .runtime import CodexCliProbeAdapter, LocalPrototypeAdapter, RuntimeAdapter, RuntimeContext
from .scheduler import Runtime
from .status import sdlc_status_payload
from .http import make_handler
from .tools import BUILTIN_TOOLS, HIGH_RISK_TOOLS
from .utils import json_dumps, json_loads, new_id, now_iso, parse_iso, token_ref


class Store:
    def __init__(
        self,
        path: str,
        max_content_bytes: int = DEFAULT_MAX_STORED_CONTENT_BYTES,
        *,
        allow_unsafe_connector_urls_for_tests: bool = False,
    ) -> None:
        self.path = path
        self.max_content_bytes = max_content_bytes
        self.adapter: RuntimeAdapter = LocalPrototypeAdapter()
        self.probe_adapter = CodexCliProbeAdapter()
        self._lock = threading.RLock()
        self._legacy_integration_secret_scrubbed = False
        # This test seam is deliberately constructor-only: production callers
        # cannot opt into unsafe connector targets through integration payloads.
        self._allow_unsafe_connector_urls_for_tests = allow_unsafe_connector_urls_for_tests
        # Connector credentials are process-local. SQLite keeps only an opaque
        # digest reference so a persisted control-plane database cannot recover
        # outbound tokens after a restart.
        self._integration_secrets: dict[str, str] = {}
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()
        self.seed_defaults()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def init_schema(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                kernel_id TEXT NOT NULL,
                system TEXT,
                metadata TEXT NOT NULL,
                version INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS environments (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                name TEXT NOT NULL,
                runtime_type TEXT NOT NULL,
                resource_class TEXT NOT NULL,
                permission_profile_id TEXT NOT NULL DEFAULT 'workspace-write',
                sandbox_profile_id TEXT NOT NULL DEFAULT 'local-subprocess-deny-all',
                network_policy TEXT NOT NULL,
                filesystem_policy TEXT NOT NULL,
                secret_policy TEXT NOT NULL,
                package_policy TEXT NOT NULL DEFAULT '{}',
                tool_policy_defaults TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                environment_id TEXT NOT NULL,
                status TEXT NOT NULL,
                turn_status TEXT,
                active_turn_id TEXT,
                last_event_id TEXT,
                vault_ids TEXT NOT NULL DEFAULT '[]',
                metadata TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                schema_version TEXT NOT NULL,
                turn_id TEXT,
                type TEXT NOT NULL,
                severity TEXT NOT NULL,
                payload TEXT NOT NULL,
                audit_ref TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(session_id, sequence)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                name TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                environment_id TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                schedule TEXT NOT NULL,
                status TEXT NOT NULL,
                next_run_at TEXT,
                last_run_at TEXT,
                metadata TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS job_runs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                job_id TEXT,
                session_id TEXT NOT NULL,
                worker_id TEXT,
                trigger_source TEXT NOT NULL,
                status TEXT NOT NULL,
                lease_expires_at TEXT,
                lease_token TEXT,
                lease_generation INTEGER NOT NULL DEFAULT 0,
                started_at TEXT,
                finished_at TEXT,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS workers (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                capabilities TEXT NOT NULL,
                active_run_id TEXT,
                last_heartbeat_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                name TEXT NOT NULL,
                size INTEGER NOT NULL,
                content_type TEXT NOT NULL,
                checksum TEXT NOT NULL,
                content TEXT NOT NULL,
                retention TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_id TEXT,
                name TEXT NOT NULL,
                size INTEGER NOT NULL,
                content_type TEXT NOT NULL,
                checksum TEXT NOT NULL,
                content TEXT NOT NULL,
                content_ref TEXT NOT NULL,
                retention TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS tool_policies (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                mode TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS pending_actions (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_id TEXT,
                tool TEXT NOT NULL,
                proposed_args TEXT NOT NULL,
                status TEXT NOT NULL,
                resolved_by TEXT,
                resolved_at TEXT,
                resolution_reason TEXT,
                execution_run_id TEXT,
                execution_lease_generation INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS usage_records (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_id TEXT,
                run_id TEXT,
                worker_id TEXT,
                token_input INTEGER NOT NULL,
                token_output INTEGER NOT NULL,
                tool_duration_ms INTEGER NOT NULL,
                sandbox_cpu_ms INTEGER NOT NULL,
                sandbox_memory_peak_mb INTEGER NOT NULL,
                sandbox_disk_read_bytes INTEGER NOT NULL,
                sandbox_disk_write_bytes INTEGER NOT NULL,
                sandbox_network_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS vaults (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                metadata TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS vault_credentials (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                vault_id TEXT NOT NULL,
                auth_type TEXT NOT NULL,
                mcp_server_url TEXT,
                secret_ref TEXT,
                auth_metadata TEXT NOT NULL DEFAULT '{}',
                metadata TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS integrations (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                name TEXT NOT NULL,
                base_url TEXT,
                secret_ref TEXT,
                status TEXT NOT NULL,
                credential_status TEXT NOT NULL DEFAULT 'registration_required',
                capabilities TEXT NOT NULL,
                metadata TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
        ]
        with self._lock:
            for statement in statements:
                self.conn.execute(statement)
            self.ensure_schema_columns()
            self.conn.commit()
            if self._legacy_integration_secret_scrubbed:
                # DROP COLUMN removes the schema surface; VACUUM rewrites pages
                # so old plaintext is not retained in the SQLite freelist.
                self.conn.execute("VACUUM")
                self._legacy_integration_secret_scrubbed = False

    def ensure_schema_columns(self) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(job_runs)").fetchall()
        }
        if "lease_token" not in columns:
            self.conn.execute("ALTER TABLE job_runs ADD COLUMN lease_token TEXT")
        if "lease_generation" not in columns:
            self.conn.execute(
                "ALTER TABLE job_runs ADD COLUMN lease_generation INTEGER NOT NULL DEFAULT 0"
            )
        environment_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(environments)").fetchall()
        }
        if "permission_profile_id" not in environment_columns:
            self.conn.execute(
                "ALTER TABLE environments ADD COLUMN permission_profile_id TEXT NOT NULL DEFAULT 'workspace-write'"
            )
        if "sandbox_profile_id" not in environment_columns:
            self.conn.execute(
                "ALTER TABLE environments ADD COLUMN sandbox_profile_id TEXT NOT NULL DEFAULT 'local-subprocess-deny-all'"
            )
        if "package_policy" not in environment_columns:
            self.conn.execute("ALTER TABLE environments ADD COLUMN package_policy TEXT NOT NULL DEFAULT '{}'")
        if "tool_policy_defaults" not in environment_columns:
            self.conn.execute(
                "ALTER TABLE environments ADD COLUMN tool_policy_defaults TEXT NOT NULL DEFAULT '{}'"
            )
        self.conn.execute(
            """
            UPDATE environments
            SET tool_policy_defaults = ?
            WHERE tool_policy_defaults IS NULL OR tool_policy_defaults = '{}'
            """,
            (json_dumps(BASE_TOOL_POLICY_DEFAULTS),),
        )
        self.conn.execute(
            """
            UPDATE environments
            SET package_policy = ?
            WHERE package_policy IS NULL OR package_policy = '{}'
            """,
            (json_dumps({"mode": "none", "allow_runtime_install": False}),),
        )
        session_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "vault_ids" not in session_columns:
            self.conn.execute("ALTER TABLE sessions ADD COLUMN vault_ids TEXT NOT NULL DEFAULT '[]'")
        vault_credential_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(vault_credentials)").fetchall()
        }
        if "auth_metadata" not in vault_credential_columns:
            self.conn.execute(
                "ALTER TABLE vault_credentials ADD COLUMN auth_metadata TEXT NOT NULL DEFAULT '{}'"
            )
        if "secret_material" in vault_credential_columns:
            self.conn.execute(
                "UPDATE vault_credentials SET secret_material = NULL WHERE secret_material IS NOT NULL"
            )
        integration_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(integrations)").fetchall()
        }
        if "credential_status" not in integration_columns:
            self.conn.execute(
                "ALTER TABLE integrations ADD COLUMN credential_status TEXT NOT NULL DEFAULT 'registration_required'"
            )
        if "secret_material" in integration_columns:
            self.conn.execute(
                "UPDATE integrations SET secret_material = NULL WHERE secret_material IS NOT NULL"
            )
            self.conn.execute("ALTER TABLE integrations DROP COLUMN secret_material")
            self._legacy_integration_secret_scrubbed = True
        self.conn.execute(
            "UPDATE integrations SET credential_status = 'registration_required', "
            "status = CASE WHEN provider = 'webhook' THEN 'credential_required' "
            "WHEN base_url IS NULL OR base_url = '' THEN 'metadata_only' "
            "ELSE 'credential_required' END"
        )
        pending_action_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(pending_actions)").fetchall()
        }
        if "execution_run_id" not in pending_action_columns:
            self.conn.execute("ALTER TABLE pending_actions ADD COLUMN execution_run_id TEXT")
        if "execution_lease_generation" not in pending_action_columns:
            self.conn.execute(
                "ALTER TABLE pending_actions ADD COLUMN execution_lease_generation INTEGER"
            )

    def seed_defaults(self) -> None:
        if self.list_environments():
            return
        self.create_environment(
            {
                "name": "local-subprocess-deny-all",
                "runtime_type": "local-subprocess",
                "resource_class": {"cpu_limit": 1, "memory_mb": 512, "disk_mb": 1024},
                "permission_profile_id": "workspace-write",
                "sandbox_profile_id": "local-subprocess-deny-all",
                "network_policy": {
                    "mode": "deny_all",
                    "allow_hosts": [],
                    "deny_private_networks": True,
                    "deny_metadata_ip": True,
                },
                "filesystem_policy": {
                    "workspace_root": "/workspace",
                    "writable_paths": ["/workspace"],
                    "read_only_root": True,
                    "allow_host_mounts": False,
                    "allow_docker_socket": False,
                },
                "secret_policy": {
                    "mode": "none",
                    "allow_runtime_injection": False,
                    "allow_model_visible_plaintext": False,
                },
                "package_policy": {"mode": "none", "allow_runtime_install": False},
                "tool_policy_defaults": BASE_TOOL_POLICY_DEFAULTS,
            },
            request_id="seed",
        )

    def audit(
        self,
        action: str,
        target_type: str,
        target_id: str,
        request_id: str,
        actor: str = "api",
    ) -> str:
        timestamp = now_iso()
        audit_id = new_id("audit")
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO audit_log
                (id, tenant_id, project_id, actor, action, target_type, target_id, request_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    DEFAULT_TENANT_ID,
                    DEFAULT_PROJECT_ID,
                    actor,
                    action,
                    target_type,
                    target_id,
                    request_id,
                    timestamp,
                ),
            )
            self.conn.commit()
        return audit_id

    def fetch_one(self, query: str, args: tuple[Any, ...]) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(query, args).fetchone()

    def fetch_all(self, query: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(query, args).fetchall())

    def list_kernels(self) -> list[dict[str, Any]]:
        return [
            self.kernel_provider_from_manifest(self.adapter.manifest(), active=True),
            self.kernel_provider_from_manifest(self.probe_adapter.manifest(), active=False),
        ]

    def list_permission_profiles(self) -> list[dict[str, Any]]:
        return list_permission_profiles()

    def get_permission_profile(self, profile_id: str) -> dict[str, Any]:
        return get_permission_profile(profile_id)

    def list_sandbox_profiles(self) -> list[dict[str, Any]]:
        return list_sandbox_profiles()

    def get_sandbox_profile(self, profile_id: str) -> dict[str, Any]:
        return get_sandbox_profile(profile_id)

    def get_kernel(self, kernel_id: str) -> dict[str, Any]:
        for kernel in self.list_kernels():
            if kernel["id"] == kernel_id:
                return kernel
        raise KeyError(kernel_id)

    def probe_kernel(self, kernel_id: str) -> dict[str, Any]:
        if kernel_id != self.probe_adapter.kernel_id:
            raise KeyError(kernel_id)
        return {
            "type": "kernel_probe",
            "kernel_id": kernel_id,
            "adapter_id": self.probe_adapter.adapter_id,
            "dry_run": True,
            "probe": self.probe_adapter.probe(),
        }

    def kernel_provider_from_manifest(self, manifest: dict[str, Any], active: bool) -> dict[str, Any]:
        probe = manifest.get("probe") or {}
        if active:
            status = "degraded"
            note = "Local prototype adapter emits normalized events; real Codex CLI execution is not attached yet."
        else:
            status = "available" if probe.get("available") else "unavailable"
            note = "Probe-only Codex CLI adapter. It runs codex --version only and does not execute prompts."
        return {
            "id": manifest["kernel_id"],
            "type": "kernel_provider",
            "tenant_id": DEFAULT_TENANT_ID,
            "project_id": DEFAULT_PROJECT_ID,
            "adapter_type": "cli",
            "runtime_mode": manifest["runtime_mode"],
            "adapter_id": manifest["adapter_id"],
            "capabilities": manifest["capabilities"],
            "constraints": manifest["constraints"],
            "status": status,
            "active": active,
            "probe": probe or None,
            "note": note,
        }

    def create_agent(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        if not payload.get("name"):
            raise ValueError("name is required")
        timestamp = now_iso()
        agent_id = new_id("agent")
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO agents
                (id, tenant_id, project_id, name, description, kernel_id, system, metadata,
                 version, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    DEFAULT_TENANT_ID,
                    DEFAULT_PROJECT_ID,
                    payload["name"],
                    payload.get("description", ""),
                    payload.get("kernel", {}).get("id", "codex-cli-local"),
                    payload.get("system", ""),
                    json_dumps(payload.get("metadata", {})),
                    1,
                    "active",
                    timestamp,
                    timestamp,
                ),
            )
            self.conn.commit()
        self.audit("agent.create", "agent", agent_id, request_id)
        return self.get_agent(agent_id)

    def list_agents(self) -> list[dict[str, Any]]:
        rows = self.fetch_all("SELECT * FROM agents WHERE archived_at IS NULL ORDER BY created_at DESC")
        return [self.agent_from_row(row) for row in rows]

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM agents WHERE id = ?", (agent_id,))
        if row is None:
            raise KeyError(agent_id)
        return self.agent_from_row(row)

    def agent_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "type": "agent",
            "tenant_id": row["tenant_id"],
            "project_id": row["project_id"],
            "name": row["name"],
            "description": row["description"],
            "kernel": {"id": row["kernel_id"]},
            "system": row["system"],
            "metadata": json_loads(row["metadata"], {}),
            "version": row["version"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "archived_at": row["archived_at"],
        }

    def create_environment(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        if not payload.get("name"):
            raise ValueError("name is required")
        timestamp = now_iso()
        env_id = new_id("env")
        resource_class = payload.get(
            "resource_class", {"cpu_limit": 1, "memory_mb": 512, "disk_mb": 1024}
        )
        permission_profile_id = payload.get("permission_profile_id", "workspace-write")
        sandbox_profile_id = payload.get("sandbox_profile_id", "local-subprocess-deny-all")
        validate_profile_for_environment(permission_profile_id)
        sandbox_profile = validate_sandbox_for_environment(sandbox_profile_id)
        runtime_type = payload.get("runtime_type", "local-subprocess")
        if sandbox_profile.get("provider") != runtime_type:
            raise ValueError(
                f"sandbox profile provider must match runtime_type: "
                f"{sandbox_profile.get('provider')} != {runtime_type}"
            )
        tool_policy_defaults = merge_tool_policy_defaults(
            permission_profile_id,
            payload.get("tool_policy_defaults") or {},
        )
        policy_defaults = environment_policy_defaults(
            permission_profile_id,
            sandbox_profile_id,
        )
        network_policy = payload.get("network_policy", policy_defaults["network_policy"])
        filesystem_policy = payload.get(
            "filesystem_policy", policy_defaults["filesystem_policy"]
        )
        secret_policy = payload.get("secret_policy", policy_defaults["secret_policy"])
        package_policy = payload.get("package_policy", policy_defaults["package_policy"])
        validate_environment_policies(
            permission_profile_id,
            sandbox_profile_id,
            network_policy,
            filesystem_policy,
            secret_policy,
            package_policy,
        )
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO environments
                (id, tenant_id, project_id, name, runtime_type, resource_class,
                 permission_profile_id, sandbox_profile_id, network_policy,
                 filesystem_policy, secret_policy, package_policy, tool_policy_defaults,
                 status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    env_id,
                    DEFAULT_TENANT_ID,
                    DEFAULT_PROJECT_ID,
                    payload["name"],
                    runtime_type,
                    json_dumps(resource_class),
                    permission_profile_id,
                    sandbox_profile_id,
                    json_dumps(network_policy),
                    json_dumps(filesystem_policy),
                    json_dumps(secret_policy),
                    json_dumps(package_policy),
                    json_dumps(tool_policy_defaults),
                    "active",
                    timestamp,
                    timestamp,
                ),
            )
            self.conn.commit()
        self.audit("environment.create", "environment", env_id, request_id)
        return self.get_environment(env_id)

    def list_environments(self) -> list[dict[str, Any]]:
        rows = self.fetch_all(
            "SELECT * FROM environments WHERE archived_at IS NULL ORDER BY created_at DESC"
        )
        return [self.environment_from_row(row) for row in rows]

    def get_environment(self, environment_id: str) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM environments WHERE id = ?", (environment_id,))
        if row is None:
            raise KeyError(environment_id)
        return self.environment_from_row(row)

    def environment_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "type": "environment",
            "tenant_id": row["tenant_id"],
            "project_id": row["project_id"],
            "name": row["name"],
            "runtime_type": row["runtime_type"],
            "resource_class": json_loads(row["resource_class"], {}),
            "permission_profile_id": row["permission_profile_id"],
            "sandbox_profile_id": row["sandbox_profile_id"],
            "network_policy": json_loads(row["network_policy"], {}),
            "filesystem_policy": json_loads(row["filesystem_policy"], {}),
            "secret_policy": json_loads(row["secret_policy"], {}),
            "package_policy": json_loads(row["package_policy"], {}),
            "tool_policy_defaults": json_loads(row["tool_policy_defaults"], {}),
            "permission_profile": get_permission_profile(row["permission_profile_id"]),
            "sandbox_profile": get_sandbox_profile(row["sandbox_profile_id"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "archived_at": row["archived_at"],
        }

    def create_session(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        agent_id = payload.get("agent", {}).get("id") or payload.get("agent_id")
        environment_id = payload.get("environment", {}).get("id") or payload.get("environment_id")
        if not agent_id:
            agents = self.list_agents()
            if not agents:
                raise ValueError("agent is required")
            agent_id = agents[0]["id"]
        if not environment_id:
            environments = self.list_environments()
            if not environments:
                raise ValueError("environment is required")
            environment_id = environments[0]["id"]

        self.get_agent(agent_id)
        self.get_environment(environment_id)
        vault_ids = payload.get("vault_ids", [])
        if vault_ids is None:
            vault_ids = []
        if not isinstance(vault_ids, list) or not all(isinstance(item, str) for item in vault_ids):
            raise ValueError("vault_ids must be a list of vault ids")
        for vault_id in vault_ids:
            vault = self.get_vault(vault_id)
            if vault["status"] != "active":
                raise ValueError(f"vault is not active: {vault_id}")
        timestamp = now_iso()
        session_id = new_id("sess")
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO sessions
                (id, tenant_id, project_id, agent_id, environment_id, status, turn_status,
                 vault_ids, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    DEFAULT_TENANT_ID,
                    DEFAULT_PROJECT_ID,
                    agent_id,
                    environment_id,
                    "idle",
                    "idle",
                    json_dumps(vault_ids),
                    json_dumps(payload.get("metadata", {})),
                    timestamp,
                    timestamp,
                ),
            )
            self.conn.commit()
        self.append_event(
            session_id,
            "session.created",
            {"agent_id": agent_id, "environment_id": environment_id},
            request_id,
        )
        self.audit("session.create", "session", session_id, request_id)
        return self.get_session(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        rows = self.fetch_all(
            "SELECT * FROM sessions WHERE archived_at IS NULL ORDER BY created_at DESC"
        )
        return [self.session_from_row(row) for row in rows]

    def get_session(self, session_id: str) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if row is None:
            raise KeyError(session_id)
        return self.session_from_row(row)

    def session_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "type": "session",
            "tenant_id": row["tenant_id"],
            "project_id": row["project_id"],
            "agent_snapshot": {"id": row["agent_id"]},
            "environment_snapshot": {"id": row["environment_id"]},
            "kernel_id": "codex-cli-local",
            "status": row["status"],
            "turn_status": row["turn_status"],
            "active_turn_id": row["active_turn_id"],
            "last_event_id": row["last_event_id"],
            "vault_ids": json_loads(row["vault_ids"], []),
            "metadata": json_loads(row["metadata"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "archived_at": row["archived_at"],
        }

    def append_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        request_id: str,
        severity: str = "info",
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        self.get_session(session_id)
        timestamp = now_iso()
        event_id = new_id("evt")
        audit_ref = self.audit("event.append", "event", event_id, request_id)
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS max_sequence FROM events WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            sequence = int(row["max_sequence"]) + 1
            self.conn.execute(
                """
                INSERT INTO events
                (id, tenant_id, project_id, session_id, sequence, schema_version,
                 turn_id, type, severity, payload, audit_ref, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    DEFAULT_TENANT_ID,
                    DEFAULT_PROJECT_ID,
                    session_id,
                    sequence,
                    SCHEMA_VERSION,
                    turn_id,
                    event_type,
                    severity,
                    json_dumps(payload),
                    audit_ref,
                    timestamp,
                ),
            )
            self.conn.execute(
                """
                UPDATE sessions SET last_event_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (event_id, timestamp, session_id),
            )
            self.conn.commit()
        return self.event_from_row(self.fetch_one("SELECT * FROM events WHERE id = ?", (event_id,)))

    def list_events(self, session_id: str, after_id: str | None = None) -> list[dict[str, Any]]:
        self.get_session(session_id)
        if after_id:
            after = self.fetch_one(
                "SELECT sequence FROM events WHERE session_id = ? AND id = ?",
                (session_id, after_id),
            )
            if after is None:
                sequence = 0
            else:
                sequence = int(after["sequence"])
            rows = self.fetch_all(
                "SELECT * FROM events WHERE session_id = ? AND sequence > ? ORDER BY sequence",
                (session_id, sequence),
            )
        else:
            rows = self.fetch_all(
                "SELECT * FROM events WHERE session_id = ? ORDER BY sequence",
                (session_id,),
            )
        return [self.event_from_row(row) for row in rows]

    def event_from_row(self, row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise KeyError("event")
        return {
            "id": row["id"],
            "sequence": row["sequence"],
            "schema_version": row["schema_version"],
            "tenant_id": row["tenant_id"],
            "project_id": row["project_id"],
            "session_id": row["session_id"],
            "turn_id": row["turn_id"],
            "type": row["type"],
            "severity": row["severity"],
            "payload": json_loads(row["payload"], {}),
            "audit_ref": row["audit_ref"],
            "created_at": row["created_at"],
        }

    def create_file(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        name = payload.get("name")
        if not name:
            raise ValueError("name is required")
        content = str(payload.get("content", ""))
        content_type = payload.get("content_type", "text/plain; charset=utf-8")
        raw = content.encode("utf-8")
        if len(raw) > self.max_content_bytes:
            raise PayloadTooLargeError("file content is too large")
        checksum = "sha256:" + hashlib.sha256(raw).hexdigest()
        file_id = new_id("file")
        timestamp = now_iso()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO files
                (id, tenant_id, project_id, name, size, content_type, checksum, content, retention, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    DEFAULT_TENANT_ID,
                    DEFAULT_PROJECT_ID,
                    name,
                    len(raw),
                    content_type,
                    checksum,
                    content,
                    payload.get("retention", DEFAULT_RETENTION),
                    timestamp,
                ),
            )
            self.conn.commit()
        self.audit("file.create", "file", file_id, request_id)
        return self.get_file(file_id)

    def list_files(self) -> list[dict[str, Any]]:
        rows = self.fetch_all("SELECT * FROM files ORDER BY created_at DESC")
        return [self.file_from_row(row) for row in rows]

    def get_file(self, file_id: str) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM files WHERE id = ?", (file_id,))
        if row is None:
            raise KeyError(file_id)
        return self.file_from_row(row)

    def get_file_content(self, file_id: str) -> tuple[bytes, str]:
        row = self.fetch_one("SELECT content, content_type FROM files WHERE id = ?", (file_id,))
        if row is None:
            raise KeyError(file_id)
        return str(row["content"]).encode("utf-8"), row["content_type"]

    def file_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "type": "file",
            "tenant_id": row["tenant_id"],
            "project_id": row["project_id"],
            "name": row["name"],
            "size": row["size"],
            "content_type": row["content_type"],
            "checksum": row["checksum"],
            "retention": row["retention"],
            "created_at": row["created_at"],
        }

    def create_artifact(
        self,
        session_id: str,
        payload: dict[str, Any],
        request_id: str,
        turn_id: str | None = None,
        emit_event: bool = True,
    ) -> dict[str, Any]:
        self.get_session(session_id)
        name = payload.get("name") or "artifact.txt"
        content = str(payload.get("content", ""))
        content_type = payload.get("content_type", "text/plain; charset=utf-8")
        raw = content.encode("utf-8")
        if len(raw) > self.max_content_bytes:
            raise PayloadTooLargeError("artifact content is too large")
        checksum = "sha256:" + hashlib.sha256(raw).hexdigest()
        artifact_id = new_id("art")
        timestamp = now_iso()
        content_ref = f"sqlite://artifacts/{artifact_id}/content"
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO artifacts
                (id, tenant_id, project_id, session_id, turn_id, name, size, content_type,
                 checksum, content, content_ref, retention, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    DEFAULT_TENANT_ID,
                    DEFAULT_PROJECT_ID,
                    session_id,
                    turn_id,
                    name,
                    len(raw),
                    content_type,
                    checksum,
                    content,
                    content_ref,
                    payload.get("retention", DEFAULT_RETENTION),
                    timestamp,
                ),
            )
            self.conn.commit()
        self.audit("artifact.create", "artifact", artifact_id, request_id)
        artifact = self.get_artifact(artifact_id)
        if emit_event:
            self.append_event(
                session_id,
                "artifact.created",
                {
                    "artifact_id": artifact_id,
                    "name": artifact["name"],
                    "size": artifact["size"],
                    "content_ref": artifact["content_ref"],
                },
                request_id,
                turn_id=turn_id,
            )
        return artifact

    def list_artifacts(self, session_id: str | None = None) -> list[dict[str, Any]]:
        if session_id:
            self.get_session(session_id)
            rows = self.fetch_all(
                "SELECT * FROM artifacts WHERE session_id = ? ORDER BY created_at DESC",
                (session_id,),
            )
        else:
            rows = self.fetch_all("SELECT * FROM artifacts ORDER BY created_at DESC")
        return [self.artifact_from_row(row) for row in rows]

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        if row is None:
            raise KeyError(artifact_id)
        return self.artifact_from_row(row)

    def get_artifact_content(self, artifact_id: str) -> tuple[bytes, str]:
        row = self.fetch_one(
            "SELECT content, content_type FROM artifacts WHERE id = ?",
            (artifact_id,),
        )
        if row is None:
            raise KeyError(artifact_id)
        return str(row["content"]).encode("utf-8"), row["content_type"]

    def artifact_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "type": "artifact",
            "tenant_id": row["tenant_id"],
            "project_id": row["project_id"],
            "session_id": row["session_id"],
            "turn_id": row["turn_id"],
            "name": row["name"],
            "size": row["size"],
            "content_type": row["content_type"],
            "checksum": row["checksum"],
            "content_ref": row["content_ref"],
            "retention": row["retention"],
            "created_at": row["created_at"],
        }

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
        usage_id = new_id("usage")
        timestamp = now_iso()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO usage_records
                (id, tenant_id, project_id, session_id, turn_id, run_id, worker_id,
                 token_input, token_output, tool_duration_ms, sandbox_cpu_ms,
                 sandbox_memory_peak_mb, sandbox_disk_read_bytes, sandbox_disk_write_bytes,
                 sandbox_network_bytes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    usage_id,
                    DEFAULT_TENANT_ID,
                    DEFAULT_PROJECT_ID,
                    session_id,
                    turn_id,
                    run_id,
                    worker_id,
                    token_input,
                    token_output,
                    tool_duration_ms,
                    sandbox_cpu_ms,
                    sandbox_memory_peak_mb,
                    sandbox_disk_read_bytes,
                    sandbox_disk_write_bytes,
                    sandbox_network_bytes,
                    timestamp,
                ),
            )
            self.conn.commit()
        self.audit("usage.record", "usage", usage_id, request_id)
        usage = self.get_usage_record(usage_id)
        self.append_event(
            session_id,
            "usage.turn_summary",
            {"usage_id": usage_id, "token_input": token_input, "token_output": token_output},
            request_id,
            turn_id=turn_id,
        )
        return usage

    def list_usage(self, session_id: str) -> list[dict[str, Any]]:
        self.get_session(session_id)
        rows = self.fetch_all(
            "SELECT * FROM usage_records WHERE session_id = ? ORDER BY created_at DESC",
            (session_id,),
        )
        return [self.usage_from_row(row) for row in rows]

    def get_usage_record(self, usage_id: str) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM usage_records WHERE id = ?", (usage_id,))
        if row is None:
            raise KeyError(usage_id)
        return self.usage_from_row(row)

    def usage_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "project_id": row["project_id"],
            "session_id": row["session_id"],
            "turn_id": row["turn_id"],
            "run_id": row["run_id"],
            "worker_id": row["worker_id"],
            "token_input": row["token_input"],
            "token_output": row["token_output"],
            "tool_duration_ms": row["tool_duration_ms"],
            "sandbox_cpu_ms": row["sandbox_cpu_ms"],
            "sandbox_memory_peak_mb": row["sandbox_memory_peak_mb"],
            "sandbox_disk_read_bytes": row["sandbox_disk_read_bytes"],
            "sandbox_disk_write_bytes": row["sandbox_disk_write_bytes"],
            "sandbox_network_bytes": row["sandbox_network_bytes"],
            "created_at": row["created_at"],
        }

    def create_vault(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        display_name = payload.get("display_name") or payload.get("name")
        if not display_name:
            raise ValueError("display_name is required")
        vault_id = new_id("vault")
        timestamp = now_iso()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO vaults
                (id, tenant_id, project_id, display_name, metadata, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    vault_id,
                    DEFAULT_TENANT_ID,
                    DEFAULT_PROJECT_ID,
                    str(display_name),
                    json_dumps(payload.get("metadata", {})),
                    "active",
                    timestamp,
                    timestamp,
                ),
            )
            self.conn.commit()
        self.audit("vault.create", "vault", vault_id, request_id)
        return self.get_vault(vault_id)

    def list_vaults(self) -> list[dict[str, Any]]:
        rows = self.fetch_all(
            "SELECT * FROM vaults WHERE archived_at IS NULL ORDER BY created_at DESC"
        )
        return [self.vault_from_row(row) for row in rows]

    def get_vault(self, vault_id: str) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM vaults WHERE id = ?", (vault_id,))
        if row is None:
            raise KeyError(vault_id)
        return self.vault_from_row(row)

    def vault_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "type": "vault",
            "tenant_id": row["tenant_id"],
            "project_id": row["project_id"],
            "display_name": row["display_name"],
            "credentials": self.list_vault_credentials(row["id"], validate_vault=False),
            "metadata": json_loads(row["metadata"], {}),
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "archived_at": row["archived_at"],
        }

    def create_vault_credential(
        self,
        vault_id: str,
        payload: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        vault = self.get_vault(vault_id)
        if vault["status"] != "active":
            raise ValueError("vault must be active")
        auth = payload.get("auth", payload)
        if not isinstance(auth, dict):
            raise ValueError("auth must be an object")
        auth_type = auth.get("type")
        if auth_type not in {"static_bearer", "mcp_oauth", "environment_variable"}:
            raise ValueError("auth.type must be static_bearer, mcp_oauth, or environment_variable")
        secret_material = self.extract_vault_secret(auth)
        if not self.vault_secret_is_complete(auth_type, secret_material):
            raise ValueError("credential secret material is required")
        credential_id = new_id("cred")
        timestamp = now_iso()
        auth_metadata = {}
        if auth_type == "environment_variable":
            auth_metadata["name"] = secret_material["name"]
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO vault_credentials
                (id, tenant_id, project_id, vault_id, auth_type, mcp_server_url, secret_ref,
                 auth_metadata, metadata, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    credential_id,
                    DEFAULT_TENANT_ID,
                    DEFAULT_PROJECT_ID,
                    vault_id,
                    auth_type,
                    auth.get("mcp_server_url"),
                    token_ref(json_dumps(secret_material)),
                    json_dumps(auth_metadata),
                    json_dumps(payload.get("metadata", {})),
                    "reference_only",
                    timestamp,
                    timestamp,
                ),
            )
            self.conn.commit()
        self.audit("vault_credential.create", "vault_credential", credential_id, request_id)
        return self.get_vault_credential(credential_id)

    @staticmethod
    def extract_vault_secret(auth: dict[str, Any]) -> dict[str, Any]:
        auth_type = auth.get("type")
        if auth_type == "static_bearer":
            return {"token": auth.get("token")}
        if auth_type == "mcp_oauth":
            return {
                "access_token": auth.get("access_token"),
                "refresh_token": auth.get("refresh_token"),
                "client_secret": auth.get("client_secret"),
            }
        if auth_type == "environment_variable":
            return {
                "name": auth.get("name"),
                "value": auth.get("value") or auth.get("secret_value"),
            }
        return {}

    @staticmethod
    def vault_secret_is_complete(auth_type: str, secret_material: dict[str, Any]) -> bool:
        if auth_type == "static_bearer":
            return bool(secret_material.get("token"))
        if auth_type == "mcp_oauth":
            return bool(secret_material.get("access_token") or secret_material.get("refresh_token"))
        if auth_type == "environment_variable":
            return bool(secret_material.get("name") and secret_material.get("value"))
        return False

    def list_vault_credentials(self, vault_id: str, validate_vault: bool = True) -> list[dict[str, Any]]:
        if validate_vault:
            row = self.fetch_one(
                "SELECT id FROM vaults WHERE id = ? AND archived_at IS NULL",
                (vault_id,),
            )
            if row is None:
                raise KeyError(vault_id)
        rows = self.fetch_all(
            """
            SELECT * FROM vault_credentials
            WHERE vault_id = ? AND archived_at IS NULL
            ORDER BY created_at DESC
            """,
            (vault_id,),
        )
        return [self.vault_credential_from_row(row) for row in rows]

    def get_vault_credential(self, credential_id: str) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM vault_credentials WHERE id = ?", (credential_id,))
        if row is None:
            raise KeyError(credential_id)
        return self.vault_credential_from_row(row)

    def vault_credential_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        auth: dict[str, Any] = {
            "type": row["auth_type"],
            "mcp_server_url": row["mcp_server_url"],
            "secret_ref": row["secret_ref"],
        }
        auth_metadata = json_loads(row["auth_metadata"], {})
        if row["auth_type"] == "environment_variable" and auth_metadata.get("name"):
            auth["name"] = auth_metadata["name"]
        return {
            "id": row["id"],
            "type": "vault_credential",
            "tenant_id": row["tenant_id"],
            "project_id": row["project_id"],
            "vault_id": row["vault_id"],
            "auth": auth,
            "metadata": json_loads(row["metadata"], {}),
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "archived_at": row["archived_at"],
        }

    def list_tools(self) -> list[dict[str, Any]]:
        return [self.tool_descriptor(tool) for tool in BUILTIN_TOOLS]

    def tool_descriptor(
        self,
        tool: dict[str, Any],
        session_id: str | None = None,
    ) -> dict[str, Any]:
        executable = tool.get("status") == "implemented"
        if executable:
            mode, source = self.resolve_tool_policy(tool["name"], session_id=session_id)
        else:
            mode, source = "always_deny", "capability_boundary"
        return {
            "id": tool["name"],
            **dict(tool),
            "executable": executable,
            "risk": "high" if tool["name"] in HIGH_RISK_TOOLS else "standard",
            "effective_policy": {
                "mode": mode,
                "decision": policy_mode_to_decision(mode),
                "source": source,
            },
        }

    def create_tool_policy(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        scope = payload.get("scope")
        mode = payload.get("mode")
        if not scope:
            raise ValueError("scope is required")
        if mode not in {"always_allow", "always_ask", "always_deny"}:
            raise ValueError("mode must be always_allow, always_ask, or always_deny")
        tool_definition = next((item for item in BUILTIN_TOOLS if item["name"] == scope), None)
        if (
            tool_definition
            and tool_definition.get("status") != "implemented"
            and mode != "always_deny"
        ):
            raise ValueError("reference-only tools can only be explicitly denied")
        policy_id = new_id("pol")
        timestamp = now_iso()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO tool_policies
                (id, tenant_id, project_id, scope, mode, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    policy_id,
                    DEFAULT_TENANT_ID,
                    DEFAULT_PROJECT_ID,
                    scope,
                    mode,
                    timestamp,
                    timestamp,
                ),
            )
            self.conn.commit()
        self.audit("tool_policy.create", "tool_policy", policy_id, request_id)
        return self.get_tool_policy(policy_id)

    def list_tool_policies(self) -> list[dict[str, Any]]:
        rows = self.fetch_all("SELECT * FROM tool_policies ORDER BY created_at DESC")
        return [self.tool_policy_from_row(row) for row in rows]

    def get_tool_policy(self, policy_id: str) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM tool_policies WHERE id = ?", (policy_id,))
        if row is None:
            raise KeyError(policy_id)
        return self.tool_policy_from_row(row)

    def tool_policy_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "project_id": row["project_id"],
            "scope": row["scope"],
            "mode": row["mode"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def resolve_tool_policy(
        self,
        tool: str,
        session_id: str | None = None,
    ) -> tuple[str, str]:
        row = self.fetch_one(
            """
            SELECT mode FROM tool_policies
            WHERE scope = ? OR scope = '*'
            ORDER BY CASE WHEN scope = ? THEN 0 ELSE 1 END, updated_at DESC
            LIMIT 1
            """,
            (tool, tool),
        )
        source = "tool_policy" if row else ("builtin_high_risk" if tool in HIGH_RISK_TOOLS else "builtin")
        mode = row["mode"] if row else ("always_ask" if tool in HIGH_RISK_TOOLS else "always_allow")
        if row:
            policy_mode_to_decision(mode)
        if session_id:
            session = self.get_session(session_id)
            environment_id = session["environment_snapshot"]["id"]
            environment = self.get_environment(environment_id)
            environment_defaults = environment.get("tool_policy_defaults", {})
            if tool in environment_defaults:
                environment_mode = str(environment_defaults[tool])
                effective_mode = more_restrictive_policy_mode(environment_mode, mode)
                if effective_mode == environment_mode and environment_mode != mode:
                    return effective_mode, "environment"
                if not row:
                    return effective_mode, "environment" if effective_mode == environment_mode else source
                return effective_mode, source
        return mode, source

    def request_tool(self, session_id: str, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        tool_value = payload.get("tool") or payload.get("name")
        if isinstance(tool_value, dict):
            tool = tool_value.get("name")
        else:
            tool = tool_value
        if not tool:
            raise ValueError("tool is required")
        known_tools = {item["name"]: item for item in BUILTIN_TOOLS}
        tool_definition = known_tools.get(tool)
        if tool_definition is None:
            raise ValueError(f"unknown tool: {tool}")
        if tool_definition.get("status") != "implemented":
            raise ValueError(f"reference-only tool cannot be requested: {tool}")
        proposed_args = payload.get("args") or payload.get("proposed_args") or {}
        turn_id = payload.get("turn_id") or new_id("turn")
        mode, policy_source = self.resolve_tool_policy(tool, session_id=session_id)
        requested = self.append_event(
            session_id,
            "tool.requested",
            {
                "tool": tool,
                "proposed_args": proposed_args,
                "policy_mode": mode,
                "policy_source": policy_source,
                "evaluated_permission": policy_mode_to_decision(mode),
            },
            request_id,
            turn_id=turn_id,
        )
        if mode == "always_deny":
            self.append_event(
                session_id,
                "policy.violation",
                {"tool": tool, "decision": "denied"},
                request_id,
                severity="warning",
                turn_id=turn_id,
            )
            return requested
        if mode == "always_ask":
            self.create_pending_action(session_id, turn_id, tool, proposed_args, request_id)
            return requested
        action = self.create_pending_action(
            session_id,
            turn_id,
            tool,
            proposed_args,
            request_id,
            status="approved",
            resolved_by="policy",
            reason="always_allow",
            wait_for_approval=False,
        )
        self.queue_tool_action(action, request_id, source="policy")
        return requested

    def execute_tool(
        self,
        session_id: str,
        turn_id: str | None,
        tool: str,
        args: dict[str, Any],
        request_id: str,
        worker_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        result: dict[str, Any]
        if tool == "artifact.create":
            artifact = self.create_artifact(session_id, args, request_id, turn_id=turn_id)
            result = {"artifact_id": artifact["id"], "content_ref": artifact["content_ref"]}
        elif tool == "file.read":
            file_id = args.get("file_id")
            if not file_id:
                raise ValueError("file_id is required")
            result = {"file": self.get_file(file_id)}
        elif tool == "integration.dify.chat":
            result = self.invoke_dify_chat(args)
        elif tool == "integration.feishu.message":
            result = self.invoke_feishu_message(args)
        else:
            raise ValueError(f"unsupported tool execution: {tool}")
        duration_ms = int((time.monotonic() - started) * 1000)
        result_payload = {"tool": tool, "result": result, "duration_ms": duration_ms}
        if worker_id:
            result_payload["worker_id"] = worker_id
        if run_id:
            result_payload["run_id"] = run_id
        self.append_event(
            session_id,
            "tool.result",
            result_payload,
            request_id,
            turn_id=turn_id,
        )
        return result

    def create_pending_action(
        self,
        session_id: str,
        turn_id: str | None,
        tool: str,
        proposed_args: dict[str, Any],
        request_id: str,
        status: str = "pending",
        resolved_by: str | None = None,
        reason: str = "",
        wait_for_approval: bool = True,
    ) -> dict[str, Any]:
        if status not in {"pending", "approved", "rejected"}:
            raise ValueError("pending action status must be pending, approved, or rejected")
        self.get_session(session_id)
        action_id = new_id("act")
        timestamp = now_iso()
        resolved_at = timestamp if status in {"approved", "rejected"} else None
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO pending_actions
                (id, tenant_id, project_id, session_id, turn_id, tool, proposed_args,
                 status, resolved_by, resolved_at, resolution_reason, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    DEFAULT_TENANT_ID,
                    DEFAULT_PROJECT_ID,
                    session_id,
                    turn_id,
                    tool,
                    json_dumps(proposed_args),
                    status,
                    resolved_by,
                    resolved_at,
                    reason,
                    timestamp,
                    timestamp,
                ),
            )
            if wait_for_approval:
                self.conn.execute(
                    """
                    UPDATE sessions
                    SET status = ?, turn_status = ?, active_turn_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    ("waiting_approval", "waiting_approval", turn_id, timestamp, session_id),
                )
            self.conn.commit()
        self.audit("pending_action.create", "pending_action", action_id, request_id)
        if wait_for_approval:
            self.append_event(
                session_id,
                "approval.required",
                {"action_id": action_id, "tool": tool, "proposed_args": proposed_args},
                request_id,
                turn_id=turn_id,
            )
        return self.get_pending_action(action_id)

    def queue_tool_action(self, action: dict[str, Any], request_id: str, source: str) -> dict[str, Any]:
        action_id = action["id"]
        created_run = False
        recovered_orphan_binding = False
        run_id: str | None = None
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT * FROM pending_actions WHERE id = ?", (action_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(action_id)
                action = self.pending_action_from_row(row)
                if action["status"] != "approved":
                    raise ValueError("tool action must be approved before it can be queued")
                run_id = action.get("execution_run_id")
                run_exists = bool(
                    run_id
                    and self.conn.execute("SELECT 1 FROM job_runs WHERE id = ?", (run_id,)).fetchone()
                )
                if not run_exists:
                    timestamp = now_iso()
                    recovered_orphan_binding = bool(run_id)
                    if not run_id:
                        run_id = new_id("run")
                        bound = self.conn.execute(
                            """
                            UPDATE pending_actions
                            SET execution_run_id = ?, updated_at = ?
                            WHERE id = ? AND status = 'approved' AND execution_run_id IS NULL
                            """,
                            (run_id, timestamp, action_id),
                        ).rowcount
                        if bound != 1:
                            raise ValueError("tool action is no longer queueable")
                    self.conn.execute(
                        """
                        INSERT INTO job_runs
                        (id, tenant_id, project_id, job_id, session_id, worker_id, trigger_source,
                         status, lease_expires_at, lease_token, lease_generation, result, created_at, updated_at, started_at)
                        VALUES (?, ?, ?, NULL, ?, NULL, ?, 'queued', NULL, NULL, 0, ?, ?, ?, NULL)
                        """,
                        (
                            run_id,
                            DEFAULT_TENANT_ID,
                            DEFAULT_PROJECT_ID,
                            action["session_id"],
                            f"tool:{action['tool']}",
                            json_dumps({}),
                            timestamp,
                            timestamp,
                        ),
                    )
                    self.refresh_session_run_state_locked(action["session_id"], timestamp)
                    created_run = True
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        if not run_id:
            raise ValueError("tool action is no longer queueable")
        run = self.get_run(run_id)
        if created_run:
            self.append_event(
                action["session_id"],
                "tool.execution_queued",
                {
                    "action_id": action["id"],
                    "tool": action["tool"],
                    "run_id": run["id"],
                    "source": source,
                    "recovered_orphan_binding": recovered_orphan_binding,
                },
                request_id,
                turn_id=action["turn_id"],
            )
            self.audit("tool.execution_enqueue", "pending_action", action["id"], request_id)
        else:
            # A retry after a crash between the transaction commit and event/audit
            # emission leaves durable reconciliation evidence instead of silently
            # treating the already-bound run as complete.
            self.append_event(
                action["session_id"],
                "tool.execution_queue_reconciled",
                {"action_id": action["id"], "tool": action["tool"], "run_id": run["id"], "source": source},
                request_id,
                turn_id=action["turn_id"],
            )
            self.audit("tool.execution_reconcile", "pending_action", action["id"], request_id)
        return {
            "type": "tool_execution_queued",
            "action": self.get_pending_action(action["id"]),
            "run": self.get_run(run["id"]),
            "session": self.get_session(action["session_id"]),
        }

    def list_pending_actions(self, session_id: str | None = None) -> list[dict[str, Any]]:
        if session_id:
            self.get_session(session_id)
            rows = self.fetch_all(
                "SELECT * FROM pending_actions WHERE session_id = ? ORDER BY created_at DESC",
                (session_id,),
            )
        else:
            rows = self.fetch_all("SELECT * FROM pending_actions ORDER BY created_at DESC")
        return [self.pending_action_from_row(row) for row in rows]

    def get_pending_action(self, action_id: str) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM pending_actions WHERE id = ?", (action_id,))
        if row is None:
            raise KeyError(action_id)
        return self.pending_action_from_row(row)

    def resolve_pending_action(
        self,
        session_id: str,
        action_id: str,
        payload: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        action = self.get_pending_action(action_id)
        if action["session_id"] != session_id:
            raise KeyError(action_id)
        decision = payload.get("decision")
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        if action["status"] != "pending":
            if decision == "approve" and action["status"] == "approved":
                self.queue_tool_action(action, request_id, source="approval_recovery")
                return self.get_pending_action(action_id)
            raise ValueError("pending action is already resolved")
        status = "approved" if decision == "approve" else "rejected"
        timestamp = now_iso()
        with self._lock:
            updated = self.conn.execute(
                """
                UPDATE pending_actions
                SET status = ?, resolved_by = ?, resolved_at = ?, resolution_reason = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    status,
                    payload.get("resolved_by", "api"),
                    timestamp,
                    payload.get("reason", ""),
                    timestamp,
                    action_id,
                ),
            ).rowcount
            if updated != 1:
                self.conn.rollback()
                raise ValueError("pending action is already resolved")
            pending_count = int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM pending_actions WHERE session_id = ? AND status = 'pending'",
                    (session_id,),
                ).fetchone()[0]
            )
            if pending_count:
                self.conn.execute(
                    """
                    UPDATE sessions
                    SET status = 'waiting_approval', turn_status = 'waiting_approval', updated_at = ?
                    WHERE id = ?
                    """,
                    (timestamp, session_id),
                )
            else:
                self.refresh_session_run_state_locked(session_id, timestamp)
            self.conn.commit()
        self.audit("pending_action.resolve", "pending_action", action_id, request_id)
        self.append_event(
            session_id,
            "approval.resolved",
            {"action_id": action_id, "decision": decision, "reason": payload.get("reason", "")},
            request_id,
            turn_id=action["turn_id"],
        )
        if decision == "approve":
            self.queue_tool_action(self.get_pending_action(action_id), request_id, source="approval")
        else:
            self.append_event(
                session_id,
                "tool.result",
                {"tool": action["tool"], "result": {"status": "rejected"}},
                request_id,
                turn_id=action["turn_id"],
            )
        return self.get_pending_action(action_id)

    def pending_action_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "project_id": row["project_id"],
            "session_id": row["session_id"],
            "turn_id": row["turn_id"],
            "tool": row["tool"],
            "proposed_args": json_loads(row["proposed_args"], {}),
            "status": row["status"],
            "resolved_by": row["resolved_by"],
            "resolved_at": row["resolved_at"],
            "reason": row["resolution_reason"],
            "execution_run_id": row["execution_run_id"],
            "execution_lease_generation": row["execution_lease_generation"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def runtime_policy_for_session(self, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        environment_id = session["environment_snapshot"]["id"]
        environment = self.get_environment(environment_id)
        validate_environment_policies(
            environment["permission_profile_id"],
            environment["sandbox_profile_id"],
            environment["network_policy"],
            environment["filesystem_policy"],
            environment["secret_policy"],
            environment["package_policy"],
        )
        return {
            "environment_id": environment["id"],
            "environment_name": environment["name"],
            "runtime_type": environment["runtime_type"],
            "permission_profile_id": environment["permission_profile_id"],
            "sandbox_profile_id": environment["sandbox_profile_id"],
            "permission_profile": environment["permission_profile"],
            "sandbox_profile": environment["sandbox_profile"],
            "vault_ids": session["vault_ids"],
            "vaults_bound_count": len(session["vault_ids"]),
            "resource_class": environment["resource_class"],
            "network_policy": environment["network_policy"],
            "filesystem_policy": environment["filesystem_policy"],
            "secret_policy": environment["secret_policy"],
            "package_policy": environment["package_policy"],
            "tool_policy_defaults": environment["tool_policy_defaults"],
        }

    def run_adapter_turn(
        self,
        session_id: str,
        source: str,
        request_id: str,
        run_id: str | None = None,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        turn_id = new_id("turn")
        with self._lock:
            self.conn.execute(
                """
                UPDATE sessions
                SET status = ?, turn_status = ?, active_turn_id = ?, updated_at = ?
                WHERE id = ?
                """,
                ("running", "running", turn_id, timestamp, session_id),
            )
            self.conn.commit()
        self.append_event(
            session_id,
            "session.running",
            {"source": source, "turn_id": turn_id, "run_id": run_id, "worker_id": worker_id},
            request_id,
            turn_id=turn_id,
        )
        adapter_result = self.adapter.execute_turn(
            self,
            RuntimeContext(
                session_id=session_id,
                turn_id=turn_id,
                source=source,
                request_id=request_id,
                adapter_id=self.adapter.adapter_id,
                kernel_id=self.adapter.kernel_id,
                run_id=run_id,
                worker_id=worker_id,
                runtime_policy=self.runtime_policy_for_session(session_id),
            ),
        )
        if run_id is None:
            timestamp = now_iso()
            with self._lock:
                self.conn.execute(
                    """
                    UPDATE sessions
                    SET status = ?, turn_status = ?, active_turn_id = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    ("idle", "idle", timestamp, session_id),
                )
                self.conn.commit()
            self.append_event(session_id, "session.idle", {"turn_id": turn_id}, request_id, turn_id=turn_id)
        return {"turn_id": turn_id, "adapter": adapter_result}

    def run_noop_turn(self, session_id: str, source: str, request_id: str) -> None:
        self.run_adapter_turn(session_id, source, request_id)

    def enqueue_session_turn(
        self,
        session_id: str,
        triggering_event: dict[str, Any],
        request_id: str,
        source: str = "api",
    ) -> dict[str, Any]:
        if triggering_event.get("session_id") != session_id:
            raise ValueError("triggering event does not belong to session")
        event_type = triggering_event["type"]
        run = self.create_run(
            session_id,
            request_id,
            trigger_source=f"session:{event_type}",
            job_id=None,
            assign_worker=False,
        )
        self.append_event(
            session_id,
            "session.turn_queued",
            {
                "event_id": triggering_event["id"],
                "event_type": event_type,
                "run_id": run["id"],
                "source": source,
            },
            request_id,
        )
        self.audit("session.turn_enqueue", "session", session_id, request_id)
        return {
            "type": "session_turn_queued",
            "event": triggering_event,
            "run": self.get_run(run["id"]),
            "session": self.get_session(session_id),
        }

    def create_job(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        if not payload.get("name"):
            raise ValueError("name is required")
        agent_id = payload.get("agent_id")
        environment_id = payload.get("environment_id")
        if not agent_id:
            agents = self.list_agents()
            if not agents:
                raise ValueError("agent_id is required")
            agent_id = agents[0]["id"]
        if not environment_id:
            environments = self.list_environments()
            if not environments:
                raise ValueError("environment_id is required")
            environment_id = environments[0]["id"]
        self.get_agent(agent_id)
        self.get_environment(environment_id)

        trigger = payload.get("trigger", {"type": "manual"})
        trigger_type = trigger.get("type", "manual")
        next_run_at = None
        if trigger_type == "delay":
            delay_seconds = int(trigger.get("delay_seconds", 0))
            next_run_at = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()
        timestamp = now_iso()
        job_id = new_id("job")
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO jobs
                (id, tenant_id, project_id, name, agent_id, environment_id, trigger_type,
                 schedule, status, next_run_at, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    DEFAULT_TENANT_ID,
                    DEFAULT_PROJECT_ID,
                    payload["name"],
                    agent_id,
                    environment_id,
                    trigger_type,
                    json_dumps(trigger),
                    "active",
                    next_run_at,
                    json_dumps(payload.get("metadata", {})),
                    timestamp,
                    timestamp,
                ),
            )
            self.conn.commit()
        self.audit("job.create", "job", job_id, request_id)
        return self.get_job(job_id)

    def register_worker(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        timestamp = now_iso()
        worker_id = payload.get("id") or new_id("worker")
        capabilities = payload.get(
            "capabilities",
            {
                "local_noop_turn": True,
                "session_events": True,
                "integration_webhooks": True,
            },
        )
        with self._lock:
            existing = self.conn.execute(
                "SELECT id FROM workers WHERE id = ?", (worker_id,)
            ).fetchone()
            if existing:
                self.conn.execute(
                    """
                    UPDATE workers
                    SET name = ?, status = ?, capabilities = ?, last_heartbeat_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        payload.get("name", worker_id),
                        payload.get("status", "active"),
                        json_dumps(capabilities),
                        timestamp,
                        timestamp,
                        worker_id,
                    ),
                )
            else:
                self.conn.execute(
                    """
                    INSERT INTO workers
                    (id, tenant_id, project_id, name, status, capabilities,
                     last_heartbeat_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        worker_id,
                        DEFAULT_TENANT_ID,
                        DEFAULT_PROJECT_ID,
                        payload.get("name", worker_id),
                        payload.get("status", "active"),
                        json_dumps(capabilities),
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
            self.conn.commit()
        self.audit("worker.register", "worker", worker_id, request_id)
        return self.get_worker(worker_id)

    def heartbeat_worker(self, worker_id: str, request_id: str) -> dict[str, Any]:
        timestamp = now_iso()
        with self._lock:
            updated = self.conn.execute(
                """
                UPDATE workers
                SET status = 'active', last_heartbeat_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, worker_id),
            ).rowcount
            self.conn.commit()
        if not updated:
            raise KeyError(worker_id)
        self.audit("worker.heartbeat", "worker", worker_id, request_id)
        return self.get_worker(worker_id)

    def list_workers(self) -> list[dict[str, Any]]:
        rows = self.fetch_all("SELECT * FROM workers ORDER BY updated_at DESC")
        return [self.worker_from_row(row) for row in rows]

    def get_worker(self, worker_id: str) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM workers WHERE id = ?", (worker_id,))
        if row is None:
            raise KeyError(worker_id)
        return self.worker_from_row(row)

    def worker_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "type": "worker",
            "tenant_id": row["tenant_id"],
            "project_id": row["project_id"],
            "name": row["name"],
            "status": row["status"],
            "capabilities": json_loads(row["capabilities"], {}),
            "active_run_id": row["active_run_id"],
            "last_heartbeat_at": row["last_heartbeat_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def select_worker(self) -> dict[str, Any] | None:
        row = self.fetch_one(
            """
            SELECT * FROM workers
            WHERE status = 'active'
              AND active_run_id IS NULL
            ORDER BY last_heartbeat_at DESC
            LIMIT 1
            """,
            (),
        )
        return self.worker_from_row(row) if row else None

    def list_jobs(self) -> list[dict[str, Any]]:
        rows = self.fetch_all("SELECT * FROM jobs ORDER BY created_at DESC")
        return [self.job_from_row(row) for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if row is None:
            raise KeyError(job_id)
        return self.job_from_row(row)

    def refresh_session_run_state_locked(
        self,
        session_id: str,
        timestamp: str,
        terminal_status: str = "idle",
    ) -> None:
        """Derive one session state from its durable run queue while holding ``_lock``."""
        statuses = {
            row["status"]
            for row in self.conn.execute(
                "SELECT status FROM job_runs WHERE session_id = ?", (session_id,)
            ).fetchall()
        }
        has_pending_approval = bool(
            self.conn.execute(
                "SELECT 1 FROM pending_actions WHERE session_id = ? AND status = 'pending' LIMIT 1",
                (session_id,),
            ).fetchone()
        )
        if "running" in statuses:
            status = turn_status = "running"
            clear_active_turn = False
        elif has_pending_approval:
            status = turn_status = "waiting_approval"
            clear_active_turn = False
        elif "queued" in statuses:
            status = turn_status = "queued"
            clear_active_turn = True
        else:
            status = turn_status = terminal_status
            clear_active_turn = True
        self.conn.execute(
            """
            UPDATE sessions
            SET status = ?, turn_status = ?,
                active_turn_id = CASE WHEN ? THEN NULL ELSE active_turn_id END,
                updated_at = ?
            WHERE id = ?
            """,
            (status, turn_status, clear_active_turn, timestamp, session_id),
        )

    def create_run(
        self,
        session_id: str,
        request_id: str,
        trigger_source: str,
        job_id: str | None = None,
        assign_worker: bool = False,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        run_id = run_id or new_id("run")
        worker_id = None
        status = "queued"
        lease_expires_at = None
        lease_token = None
        lease_generation = 0
        with self._lock:
            if assign_worker:
                worker_row = self.conn.execute(
                    """
                    SELECT * FROM workers
                    WHERE status = 'active'
                      AND active_run_id IS NULL
                    ORDER BY last_heartbeat_at DESC
                    LIMIT 1
                    """
                ).fetchone()
                if worker_row:
                    worker_id = worker_row["id"]
                    status = "running"
                    lease_expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
                    lease_token = new_id("lease")
                    lease_generation = 1
            self.conn.execute(
                """
                INSERT INTO job_runs
                (id, tenant_id, project_id, job_id, session_id, worker_id, trigger_source,
                 status, lease_expires_at, lease_token, lease_generation, result, created_at, updated_at, started_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    DEFAULT_TENANT_ID,
                    DEFAULT_PROJECT_ID,
                    job_id,
                    session_id,
                    worker_id,
                    trigger_source,
                    status,
                    lease_expires_at,
                    lease_token,
                    lease_generation,
                    json_dumps({}),
                    timestamp,
                    timestamp,
                    timestamp if worker_id else None,
                ),
            )
            if worker_id:
                updated_worker = self.conn.execute(
                    """
                    UPDATE workers
                    SET active_run_id = ?, updated_at = ?
                    WHERE id = ? AND active_run_id IS NULL
                    """,
                    (run_id, timestamp, worker_id),
                ).rowcount
                if updated_worker != 1:
                    worker_id = None
                    status = "queued"
                    lease_expires_at = None
                    lease_token = None
                    lease_generation = 0
                    self.conn.execute(
                        """
                        UPDATE job_runs
                        SET status = 'queued',
                            worker_id = NULL,
                            lease_expires_at = NULL,
                            lease_token = NULL,
                            lease_generation = 0,
                            started_at = NULL,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (timestamp, run_id),
                    )
            self.refresh_session_run_state_locked(session_id, timestamp)
            self.conn.commit()
        self.audit("run.create", "run", run_id, request_id)
        run = self.get_run(run_id)
        self.append_event(
            session_id,
            "run.queued" if not worker_id else "run.assigned",
            {
                "run_id": run_id,
                "job_id": job_id,
                "trigger_source": trigger_source,
                "worker_id": worker_id,
                "lease_expires_at": lease_expires_at,
                "lease_generation": lease_generation,
            },
            request_id,
        )
        return run

    def claim_next_run(
        self,
        worker_id: str,
        request_id: str,
        lease_seconds: int = 900,
    ) -> dict[str, Any]:
        if lease_seconds <= 0 or lease_seconds > 86400:
            raise ValueError("lease_seconds must be between 1 and 86400")
        worker = self.get_worker(worker_id)
        if worker.get("active_run_id"):
            raise ValueError("worker already has an active run")
        timestamp = now_iso()
        lease_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        lease_token = new_id("lease")
        with self._lock:
            worker_row = self.conn.execute(
                "SELECT * FROM workers WHERE id = ?", (worker_id,)
            ).fetchone()
            if worker_row is None:
                raise KeyError(worker_id)
            if worker_row["active_run_id"]:
                raise ValueError("worker already has an active run")
            row = self.conn.execute(
                """
                SELECT queued.* FROM job_runs AS queued
                WHERE queued.status = 'queued'
                  AND NOT EXISTS (
                      SELECT 1 FROM job_runs AS running
                      WHERE running.session_id = queued.session_id
                        AND running.status = 'running'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM pending_actions AS pending
                      WHERE pending.session_id = queued.session_id
                        AND pending.status = 'pending'
                  )
                ORDER BY queued.created_at
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                self.conn.execute(
                    """
                    UPDATE workers
                    SET status = 'active', last_heartbeat_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (timestamp, timestamp, worker_id),
                )
                self.conn.commit()
                self.audit("worker.claim.empty", "worker", worker_id, request_id)
                return {"type": "worker_claim", "worker": self.get_worker(worker_id), "run": None}
            run_id = row["id"]
            updated = self.conn.execute(
                """
                UPDATE job_runs
                SET status = 'running',
                    worker_id = ?,
                    lease_expires_at = ?,
                    lease_token = ?,
                    lease_generation = lease_generation + 1,
                    started_at = COALESCE(started_at, ?),
                    updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (worker_id, lease_expires_at, lease_token, timestamp, timestamp, run_id),
            ).rowcount
            if updated != 1:
                self.conn.commit()
                raise ValueError("queued run could not be claimed")
            worker_updated = self.conn.execute(
                """
                UPDATE workers
                SET status = 'active', active_run_id = ?, last_heartbeat_at = ?, updated_at = ?
                WHERE id = ? AND active_run_id IS NULL
                """,
                (run_id, timestamp, timestamp, worker_id),
            ).rowcount
            if worker_updated != 1:
                self.conn.rollback()
                raise ValueError("worker already has an active run")
            self.conn.execute(
                """
                UPDATE sessions
                SET status = ?, turn_status = ?, updated_at = ?
                WHERE id = ?
                """,
                ("starting", "starting", timestamp, row["session_id"]),
            )
            self.conn.commit()
        self.audit("worker.claim", "run", run_id, request_id)
        self.append_event(
            row["session_id"],
            "worker.claimed",
            {
                "run_id": run_id,
                "worker_id": worker_id,
                "lease_expires_at": lease_expires_at,
            },
            request_id,
        )
        return {
            "type": "worker_claim",
            "worker": self.get_worker(worker_id),
            "run": self.get_run(run_id, include_lease_token=True),
        }

    def execute_claimed_run(
        self,
        worker_id: str,
        run_id: str,
        lease_token: str | None,
        request_id: str,
    ) -> dict[str, Any]:
        run = self.validate_worker_run(worker_id, run_id, lease_token)
        if run["trigger_source"].startswith("tool:"):
            raise ValueError("tool runs must use the worker-scoped tools/execute endpoint")
        adapter_result = self.run_adapter_turn(
            run["session_id"],
            source=run["trigger_source"],
            request_id=request_id,
            run_id=run_id,
            worker_id=worker_id,
        )
        adapter_status = adapter_result["adapter"].get("status")
        final_status = "succeeded" if adapter_status == "succeeded" else "failed"
        completed = self.complete_run(
            run_id,
            final_status,
            {
                "session_id": run["session_id"],
                "completed_by": worker_id,
                "adapter": adapter_result["adapter"],
            },
            request_id,
            worker_id=worker_id,
            lease_token=lease_token,
        )
        self.append_worker_completion_event(
            run["session_id"],
            run_id,
            worker_id,
            final_status,
            adapter_result["turn_id"],
            request_id,
        )
        return {"type": "worker_run_execution", "run": completed, "adapter": adapter_result["adapter"]}

    def validate_worker_run(self, worker_id: str, run_id: str, lease_token: str | None) -> dict[str, Any]:
        run = self.get_run(run_id, include_lease_token=True)
        if run["worker_id"] != worker_id:
            raise PermissionError("run is not assigned to this worker")
        if run["status"] != "running":
            raise ValueError("run must be running")
        if not lease_token or not isinstance(lease_token, str):
            raise PermissionError("lease_token is required")
        current_token = run.get("lease_token")
        if not current_token or not hmac.compare_digest(current_token, lease_token):
            raise PermissionError("invalid run lease token")
        if run.get("lease_expires_at") and parse_iso(run["lease_expires_at"]) <= datetime.now(timezone.utc):
            raise PermissionError("run lease is expired")
        return run

    def start_worker_run_turn(
        self,
        worker_id: str,
        run_id: str,
        lease_token: str | None,
        request_id: str,
    ) -> dict[str, Any]:
        run = self.validate_worker_run(worker_id, run_id, lease_token)
        if run["trigger_source"].startswith("tool:"):
            raise ValueError("tool runs must use the worker-scoped tools/execute endpoint")
        session = self.get_session(run["session_id"])
        if session.get("active_turn_id"):
            raise ValueError("session already has an active turn")
        timestamp = now_iso()
        turn_id = new_id("turn")
        with self._lock:
            updated = self.conn.execute(
                """
                UPDATE sessions
                SET status = ?, turn_status = ?, active_turn_id = ?, updated_at = ?
                WHERE id = ? AND active_turn_id IS NULL
                """,
                ("running", "running", turn_id, timestamp, run["session_id"]),
            ).rowcount
            self.conn.commit()
        if updated != 1:
            raise ValueError("session already has an active turn")
        self.append_event(
            run["session_id"],
            "session.running",
            {"source": run["trigger_source"], "turn_id": turn_id, "run_id": run_id, "worker_id": worker_id},
            request_id,
            turn_id=turn_id,
        )
        return {
            "type": "worker_turn_start",
            "turn_id": turn_id,
            "run": self.get_run(run_id),
            "session": self.get_session(run["session_id"]),
            "adapter": self.adapter.manifest(),
            "runtime_policy": self.runtime_policy_for_session(run["session_id"]),
        }

    def renew_worker_run_lease(
        self,
        worker_id: str,
        run_id: str,
        payload: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        lease_seconds = int(payload.get("lease_seconds", 900))
        if lease_seconds <= 0 or lease_seconds > 86400:
            raise ValueError("lease_seconds must be between 1 and 86400")
        run = self.validate_worker_run(worker_id, run_id, payload.get("lease_token"))
        timestamp = now_iso()
        lease_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        with self._lock:
            updated = self.conn.execute(
                """
                UPDATE job_runs
                SET lease_expires_at = ?, updated_at = ?
                WHERE id = ?
                  AND status = 'running'
                  AND worker_id = ?
                  AND lease_token = ?
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at > ?
                """,
                (lease_expires_at, timestamp, run_id, worker_id, payload.get("lease_token"), timestamp),
            ).rowcount
            if updated != 1:
                self.conn.rollback()
                raise PermissionError("run lease is no longer current")
            self.conn.execute(
                """
                UPDATE workers
                SET status = 'active', last_heartbeat_at = ?, updated_at = ?
                WHERE id = ? AND active_run_id = ?
                """,
                (timestamp, timestamp, worker_id, run_id),
            )
            self.conn.commit()
        self.audit("worker.lease_renew", "run", run_id, request_id)
        self.append_event(
            run["session_id"],
            "worker.lease_renewed",
            {
                "run_id": run_id,
                "worker_id": worker_id,
                "lease_expires_at": lease_expires_at,
                "lease_generation": run["lease_generation"],
            },
            request_id,
        )
        return {
            "type": "worker_lease_renewal",
            "run": self.get_run(run_id, include_lease_token=True),
            "worker": self.get_worker(worker_id),
        }

    def append_worker_run_event(
        self,
        worker_id: str,
        run_id: str,
        payload: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        run = self.validate_worker_run(worker_id, run_id, payload.get("lease_token"))
        event_type = payload.get("type") or payload.get("event_type")
        if not event_type:
            raise ValueError("event type is required")
        event_payload = payload.get("payload", {})
        if not isinstance(event_payload, dict):
            raise ValueError("event payload must be an object")
        turn_id = payload.get("turn_id") or self.get_session(run["session_id"]).get("active_turn_id")
        severity = payload.get("severity", "info")
        return self.append_event(
            run["session_id"],
            event_type,
            event_payload,
            request_id,
            severity=severity,
            turn_id=turn_id,
        )

    def create_worker_run_artifact(
        self,
        worker_id: str,
        run_id: str,
        payload: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        run = self.validate_worker_run(worker_id, run_id, payload.get("lease_token"))
        artifact_payload = payload.get("artifact", payload)
        if not isinstance(artifact_payload, dict):
            raise ValueError("artifact payload must be an object")
        artifact_payload = dict(artifact_payload)
        turn_id = payload.get("turn_id") or artifact_payload.pop("turn_id", None)
        emit_event = bool(payload.get("emit_event", artifact_payload.pop("emit_event", True)))
        return self.create_artifact(
            run["session_id"],
            artifact_payload,
            request_id,
            turn_id=turn_id,
            emit_event=emit_event,
        )

    def record_worker_run_usage(
        self,
        worker_id: str,
        run_id: str,
        payload: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        run = self.validate_worker_run(worker_id, run_id, payload.get("lease_token"))
        return self.record_usage(
            run["session_id"],
            payload.get("turn_id"),
            request_id,
            run_id=run_id,
            worker_id=worker_id,
            token_input=int(payload.get("token_input", 0)),
            token_output=int(payload.get("token_output", 0)),
            tool_duration_ms=int(payload.get("tool_duration_ms", 0)),
            sandbox_cpu_ms=int(payload.get("sandbox_cpu_ms", 0)),
            sandbox_memory_peak_mb=int(payload.get("sandbox_memory_peak_mb", 0)),
            sandbox_disk_read_bytes=int(payload.get("sandbox_disk_read_bytes", 0)),
            sandbox_disk_write_bytes=int(payload.get("sandbox_disk_write_bytes", 0)),
            sandbox_network_bytes=int(payload.get("sandbox_network_bytes", 0)),
        )

    def execute_worker_run_tool(
        self,
        worker_id: str,
        run_id: str,
        payload: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        run = self.validate_worker_run(worker_id, run_id, payload.get("lease_token"))
        action_id = payload.get("action_id")
        if not action_id:
            raise ValueError("action_id is required")
        action = self.get_pending_action(action_id)
        if action["session_id"] != run["session_id"]:
            raise PermissionError("tool action does not belong to this run session")
        if run["trigger_source"] != f"tool:{action['tool']}":
            raise PermissionError("tool action is not assigned to this run")
        if action.get("execution_run_id") != run_id:
            raise PermissionError("tool action is not bound to this run")
        if action["status"] != "approved":
            if action["status"] == "executing" and action.get("execution_run_id") == run_id:
                raise ValueError("tool action is already executing")
            raise ValueError("tool action must be approved before worker execution")
        turn_id = payload.get("turn_id") or action["turn_id"] or self.get_session(run["session_id"]).get("active_turn_id")
        with self._lock:
            claimed = self.conn.execute(
                """
                UPDATE pending_actions
                SET status = 'executing', execution_lease_generation = ?, updated_at = ?
                WHERE id = ? AND status = 'approved' AND execution_run_id = ?
                """,
                (run["lease_generation"], now_iso(), action_id, run_id),
            ).rowcount
            self.conn.commit()
        if claimed != 1:
            current_action = self.get_pending_action(action_id)
            if current_action["status"] == "executing":
                raise ValueError("tool action is already executing")
            raise ValueError("tool action is no longer executable")
        try:
            self.append_event(
                run["session_id"],
                "tool.execution_started",
                {"action_id": action_id, "tool": action["tool"], "run_id": run_id, "worker_id": worker_id},
                request_id,
                turn_id=turn_id,
            )
            result = self.execute_tool(
                run["session_id"],
                turn_id,
                action["tool"],
                action["proposed_args"],
                request_id,
                worker_id=worker_id,
                run_id=run_id,
            )
        except Exception:
            failed_at = now_iso()
            with self._lock:
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    action_failed = self.conn.execute(
                    """
                    UPDATE pending_actions
                    SET status = 'failed', execution_lease_generation = NULL, updated_at = ?
                    WHERE id = ? AND status = 'executing' AND execution_run_id = ?
                      AND execution_lease_generation = ?
                    """,
                    (failed_at, action_id, run_id, run["lease_generation"]),
                    ).rowcount
                    if action_failed == 1:
                        self.conn.execute(
                            """
                            UPDATE job_runs
                            SET status = 'failed', result = ?, finished_at = ?, lease_expires_at = NULL,
                                lease_token = NULL, updated_at = ?
                            WHERE id = ? AND status = 'running' AND worker_id = ? AND lease_token = ?
                              AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                            """,
                            (
                                json_dumps({"tool_action_id": action_id, "error": "tool execution failed"}),
                                failed_at,
                                failed_at,
                                run_id,
                                worker_id,
                                payload.get("lease_token"),
                                failed_at,
                            ),
                        )
                        self.conn.execute(
                            """
                            UPDATE workers SET active_run_id = NULL, updated_at = ?
                            WHERE id = ? AND active_run_id = ?
                            """,
                            (failed_at, worker_id, run_id),
                        )
                        self.refresh_session_run_state_locked(run["session_id"], failed_at, terminal_status="failed")
                    self.conn.commit()
                except Exception:
                    self.conn.rollback()
                    raise
            self.audit("worker.tool_execute_failed", "pending_action", action_id, request_id)
            self.append_event(
                run["session_id"],
                "tool.execution_failed",
                {
                    "action_id": action_id,
                    "tool": action["tool"],
                    "run_id": run_id,
                    "worker_id": worker_id,
                },
                request_id,
                severity="error",
                turn_id=turn_id,
            )
            raise
        completed_at = now_iso()
        with self._lock:
            updated = self.conn.execute(
                """
                UPDATE pending_actions
                SET status = 'executed', updated_at = ?
                WHERE id = ? AND status = 'executing' AND execution_run_id = ?
                  AND execution_lease_generation = ?
                """,
                (completed_at, action_id, run_id, run["lease_generation"]),
            ).rowcount
            self.conn.commit()
        if updated != 1:
            raise RuntimeError("tool action execution ownership was lost")
        self.audit("worker.tool_execute", "pending_action", action_id, request_id)
        self.append_event(
            run["session_id"],
            "tool.execution_completed",
            {"action_id": action_id, "tool": action["tool"], "run_id": run_id, "worker_id": worker_id},
            request_id,
            turn_id=turn_id,
        )
        return {
            "type": "worker_tool_execution",
            "action": self.get_pending_action(action_id),
            "run": self.get_run(run_id),
            "result": result,
        }

    def complete_worker_run(
        self,
        worker_id: str,
        run_id: str,
        payload: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        lease_token = payload.get("lease_token")
        run = self.validate_worker_run(worker_id, run_id, lease_token)
        status = payload.get("status")
        if status not in {"succeeded", "failed", "canceled"}:
            raise ValueError("status must be succeeded, failed, or canceled")
        result = payload.get("result", {})
        if not isinstance(result, dict):
            raise ValueError("result must be an object")
        turn_id = payload.get("turn_id") or self.get_session(run["session_id"]).get("active_turn_id")
        completed = self.complete_run(
            run_id,
            status,
            result,
            request_id,
            worker_id=worker_id,
            lease_token=lease_token,
        )
        self.append_worker_completion_event(
            run["session_id"], run_id, worker_id, status, turn_id, request_id
        )
        return {
            "type": "worker_run_completion",
            "run": completed,
            "session": self.get_session(run["session_id"]),
        }

    def append_worker_completion_event(
        self,
        session_id: str,
        run_id: str,
        worker_id: str,
        run_status: str,
        turn_id: str | None,
        request_id: str,
    ) -> None:
        session = self.get_session(session_id)
        if session["status"] == "idle":
            event_type = "session.idle"
        elif session["status"] == "failed":
            event_type = "session.failed"
        else:
            event_type = "session.run_completed"
        self.append_event(
            session_id,
            event_type,
            {
                "turn_id": turn_id,
                "run_id": run_id,
                "worker_id": worker_id,
                "run_status": run_status,
                "session_status": session["status"],
            },
            request_id,
            severity="error" if run_status == "failed" else "info",
            turn_id=turn_id,
        )

    def requeue_expired_runs(self, request_id: str) -> int:
        timestamp = now_iso()
        rows = self.fetch_all(
            """
            SELECT * FROM job_runs
            WHERE status = 'running'
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at <= ?
            """,
            (timestamp,),
        )
        requeued = 0
        for row in rows:
            recovery_action = "requeued"
            final_status = "queued"
            with self._lock:
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    current = self.conn.execute(
                        """
                        SELECT * FROM job_runs WHERE id = ? AND status = 'running'
                          AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                        """,
                        (row["id"], timestamp),
                    ).fetchone()
                    if current is None:
                        self.conn.commit()
                        continue
                    action = self.conn.execute(
                        "SELECT * FROM pending_actions WHERE execution_run_id = ?", (row["id"],)
                    ).fetchone()
                    terminal_status: str | None = None
                    result: dict[str, Any] | None = None
                    if action is not None and action["status"] == "executing":
                        terminal_status = "failed"
                        recovery_action = "ambiguous_failed"
                        result = {
                            "tool_action_id": action["id"],
                            "error": "tool execution lease expired; external side effect outcome is ambiguous",
                        }
                        self.conn.execute(
                            """
                            UPDATE pending_actions
                            SET status = 'failed', execution_lease_generation = NULL, updated_at = ?
                            WHERE id = ? AND status = 'executing'
                            """,
                            (timestamp, action["id"]),
                        )
                    elif action is not None and action["status"] == "executed":
                        terminal_status = "succeeded"
                        recovery_action = "terminal_converged"
                        result = {"tool_action_id": action["id"], "recovered": "action_executed_before_run_completion"}
                    elif action is not None and action["status"] == "failed":
                        terminal_status = "failed"
                        recovery_action = "terminal_converged"
                        result = {"tool_action_id": action["id"], "recovered": "action_failed_before_run_completion"}
                    elif current["trigger_source"].startswith("tool:") and (
                        action is None or action["status"] != "approved"
                    ):
                        terminal_status = "failed"
                        recovery_action = "invalid_tool_run_failed"
                        result = {
                            "tool_action_id": action["id"] if action is not None else None,
                            "error": "expired tool run has no queueable action state",
                        }

                    if terminal_status:
                        final_status = terminal_status
                        self.conn.execute(
                            """
                            UPDATE job_runs
                            SET status = ?, result = ?, finished_at = ?, lease_expires_at = NULL,
                                lease_token = NULL, updated_at = ?
                            WHERE id = ? AND status = 'running'
                            """,
                            (terminal_status, json_dumps(result), timestamp, timestamp, row["id"]),
                        )
                    else:
                        self.conn.execute(
                            """
                            UPDATE job_runs
                            SET status = 'queued', worker_id = NULL, lease_expires_at = NULL,
                                lease_token = NULL, updated_at = ?
                            WHERE id = ? AND status = 'running'
                            """,
                            (timestamp, row["id"]),
                        )
                    if row["worker_id"]:
                        self.conn.execute(
                            "UPDATE workers SET active_run_id = NULL, updated_at = ? WHERE id = ? AND active_run_id = ?",
                            (timestamp, row["worker_id"], row["id"]),
                        )
                    self.refresh_session_run_state_locked(
                        row["session_id"], timestamp, terminal_status="failed" if terminal_status == "failed" else "idle"
                    )
                    self.conn.commit()
                    requeued += 1
                except Exception:
                    self.conn.rollback()
                    raise
            self.audit("run.lease_expired", "run", row["id"], request_id)
            self.append_event(
                row["session_id"],
                "worker.lease_expired",
                {
                    "run_id": row["id"],
                    "worker_id": row["worker_id"],
                    "recovery_action": recovery_action,
                    "final_status": final_status,
                },
                request_id,
                severity="warning",
            )
        return requeued

    def complete_run(
        self,
        run_id: str,
        status: str,
        result: dict[str, Any],
        request_id: str,
        worker_id: str | None = None,
        lease_token: str | None = None,
    ) -> dict[str, Any]:
        timestamp = now_iso()
        run = self.get_run(run_id)
        with self._lock:
            if run["trigger_source"].startswith("tool:"):
                action = self.conn.execute(
                    "SELECT status, tool FROM pending_actions WHERE execution_run_id = ?",
                    (run_id,),
                ).fetchone()
                expected_action_status = "executed" if status == "succeeded" else "failed"
                if (
                    action is None
                    or run["trigger_source"] != f"tool:{action['tool']}"
                    or action["status"] != expected_action_status
                ):
                    raise ValueError(
                        "tool run cannot be completed before its bound action reaches the matching terminal state"
                    )
            if worker_id is not None or lease_token is not None:
                if not worker_id or not lease_token:
                    raise PermissionError("worker_id and lease_token are required")
                updated = self.conn.execute(
                    """
                    UPDATE job_runs
                    SET status = ?,
                        result = ?,
                        finished_at = ?,
                        lease_expires_at = NULL,
                        lease_token = NULL,
                        updated_at = ?
                    WHERE id = ?
                      AND status = 'running'
                      AND worker_id = ?
                      AND lease_token = ?
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at > ?
                    """,
                    (status, json_dumps(result), timestamp, timestamp, run_id, worker_id, lease_token, timestamp),
                ).rowcount
                if updated != 1:
                    self.conn.rollback()
                    raise PermissionError("run lease is no longer current")
            else:
                self.conn.execute(
                    """
                    UPDATE job_runs
                    SET status = ?,
                        result = ?,
                        finished_at = ?,
                        lease_expires_at = NULL,
                        lease_token = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (status, json_dumps(result), timestamp, timestamp, run_id),
                )
            if run.get("worker_id"):
                self.conn.execute(
                    """
                    UPDATE workers
                    SET active_run_id = NULL, updated_at = ?
                    WHERE id = ? AND active_run_id = ?
                    """,
                    (timestamp, run["worker_id"], run_id),
                )
            self.refresh_session_run_state_locked(
                run["session_id"],
                timestamp,
                terminal_status="failed" if status == "failed" else "idle",
            )
            self.conn.commit()
        self.audit("run.complete", "run", run_id, request_id)
        completed = self.get_run(run_id)
        self.append_event(
            run["session_id"],
            "run.completed",
            {"run_id": run_id, "status": status, "worker_id": run.get("worker_id")},
            request_id,
            severity="error" if status == "failed" else "info",
        )
        return completed

    def list_runs(self, include_lease_token: bool = False) -> list[dict[str, Any]]:
        rows = self.fetch_all("SELECT * FROM job_runs ORDER BY created_at DESC")
        return [self.run_from_row(row, include_lease_token=include_lease_token) for row in rows]

    def get_run(self, run_id: str, include_lease_token: bool = False) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM job_runs WHERE id = ?", (run_id,))
        if row is None:
            raise KeyError(run_id)
        return self.run_from_row(row, include_lease_token=include_lease_token)

    def run_from_row(self, row: sqlite3.Row, include_lease_token: bool = False) -> dict[str, Any]:
        run = {
            "id": row["id"],
            "type": "job_run",
            "tenant_id": row["tenant_id"],
            "project_id": row["project_id"],
            "job_id": row["job_id"],
            "session_id": row["session_id"],
            "worker_id": row["worker_id"],
            "trigger_source": row["trigger_source"],
            "status": row["status"],
            "lease_expires_at": row["lease_expires_at"],
            "lease_generation": row["lease_generation"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "result": json_loads(row["result"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if include_lease_token:
            run["lease_token"] = row["lease_token"]
        return run

    def job_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "type": "job",
            "tenant_id": row["tenant_id"],
            "project_id": row["project_id"],
            "name": row["name"],
            "agent_id": row["agent_id"],
            "environment_id": row["environment_id"],
            "trigger": json_loads(row["schedule"], {}),
            "status": row["status"],
            "next_run_at": row["next_run_at"],
            "last_run_at": row["last_run_at"],
            "metadata": json_loads(row["metadata"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def trigger_job(self, job_id: str, request_id: str, source: str = "manual") -> dict[str, Any]:
        job = self.get_job(job_id)
        session = self.create_session(
            {"agent_id": job["agent_id"], "environment_id": job["environment_id"], "metadata": {"job_id": job_id}},
            request_id,
        )
        run = self.create_run(
            session["id"],
            request_id,
            trigger_source=f"job:{source}",
            job_id=job_id,
            assign_worker=False,
        )
        self.append_event(
            session["id"],
            "job.triggered",
            {"job_id": job_id, "run_id": run["id"], "source": source},
            request_id,
        )
        timestamp = now_iso()
        with self._lock:
            self.conn.execute(
                """
                UPDATE jobs SET last_run_at = ?, next_run_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, job_id),
            )
            self.conn.commit()
        self.audit("job.trigger", "job", job_id, request_id)
        return {"job": self.get_job(job_id), "run": self.get_run(run["id"]), "session": self.get_session(session["id"])}

    def enqueue_job(self, job_id: str, request_id: str, source: str = "manual") -> dict[str, Any]:
        job = self.get_job(job_id)
        session = self.create_session(
            {"agent_id": job["agent_id"], "environment_id": job["environment_id"], "metadata": {"job_id": job_id}},
            request_id,
        )
        run = self.create_run(
            session["id"],
            request_id,
            trigger_source=f"job:{source}",
            job_id=job_id,
            assign_worker=False,
        )
        self.append_event(
            session["id"],
            "job.enqueued",
            {"job_id": job_id, "run_id": run["id"], "source": source},
            request_id,
        )
        timestamp = now_iso()
        with self._lock:
            self.conn.execute(
                """
                UPDATE jobs SET last_run_at = ?, next_run_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, job_id),
            )
            self.conn.commit()
        self.audit("job.enqueue", "job", job_id, request_id)
        return {"job": self.get_job(job_id), "run": self.get_run(run["id"]), "session": self.get_session(session["id"])}

    def trigger_integration_webhook(
        self,
        provider: str,
        integration_id: str,
        payload: dict[str, Any],
        request_id: str,
        supplied_token: str | None,
    ) -> dict[str, Any]:
        integration = self.get_integration(integration_id)
        if integration["provider"] != provider:
            raise ValueError("webhook provider does not match integration")
        if not integration.get("secret_ref"):
            raise PermissionError("webhook token is not configured for integration")
        if not hmac.compare_digest(token_ref(supplied_token) or "", integration["secret_ref"]):
            raise PermissionError("missing or invalid webhook token")

        agents = self.list_agents()
        if not agents:
            raise ValueError("at least one agent is required before webhook trigger")
        environments = self.list_environments()
        if not environments:
            raise ValueError("at least one environment is required before webhook trigger")
        session = self.create_session(
            {
                "agent_id": agents[0]["id"],
                "environment_id": environments[0]["id"],
                "metadata": {"integration_id": integration_id, "provider": provider},
            },
            request_id,
        )
        run = self.create_run(
            session["id"],
            request_id,
            trigger_source=f"integration:{provider}",
            job_id=None,
        )
        self.append_event(
            session["id"],
            "integration.webhook.received",
            {
                "integration_id": integration_id,
                "provider": provider,
                "run_id": run["id"],
                "payload": payload,
            },
            request_id,
        )
        self.audit("integration.webhook", "integration", integration_id, request_id)
        return {
            "type": "webhook_trigger",
            "integration": {"id": integration["id"], "provider": integration["provider"]},
            "run": self.get_run(run["id"]),
            "session": self.get_session(session["id"]),
        }

    def due_jobs(self) -> list[dict[str, Any]]:
        timestamp = now_iso()
        rows = self.fetch_all(
            """
            SELECT * FROM jobs
            WHERE status = 'active' AND next_run_at IS NOT NULL AND next_run_at <= ?
            ORDER BY next_run_at
            """,
            (timestamp,),
        )
        return [self.job_from_row(row) for row in rows]

    def create_integration(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        provider = payload.get("provider")
        if not provider:
            raise ValueError("provider is required")
        if provider not in {"feishu", "dify", "github_actions", "webhook"}:
            raise ValueError("provider must be feishu, dify, github_actions, or webhook")
        self.validate_integration_base_url(payload.get("base_url"))
        timestamp = now_iso()
        integration_id = new_id("int")
        capabilities = integration_capabilities(provider)
        secret_material = payload.get("token") or payload.get("secret")
        if secret_material is not None and not isinstance(secret_material, str):
            raise ValueError("integration secret must be a string")
        has_secret = bool(secret_material)
        has_base_url = bool(payload.get("base_url"))
        status = "configured" if has_secret and (has_base_url or provider == "webhook") else (
            "credential_required" if has_base_url or provider == "webhook" else "metadata_only"
        )
        credential_status = "registered" if has_secret else "registration_required"
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO integrations
                (id, tenant_id, project_id, provider, name, base_url, secret_ref,
                 status, credential_status, capabilities, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    integration_id,
                    DEFAULT_TENANT_ID,
                    DEFAULT_PROJECT_ID,
                    provider,
                    payload.get("name") or provider,
                    payload.get("base_url"),
                    token_ref(secret_material),
                    status,
                    credential_status,
                    json_dumps(capabilities),
                    json_dumps(payload.get("metadata", {})),
                    timestamp,
                    timestamp,
                ),
            )
            if has_secret:
                self._integration_secrets[integration_id] = secret_material
            self.conn.commit()
        self.audit("integration.create", "integration", integration_id, request_id)
        return self.get_integration(integration_id)

    @staticmethod
    def validate_integration_base_url(base_url: Any) -> None:
        if base_url is None or base_url == "":
            return
        if not isinstance(base_url, str):
            raise ValueError("integration base_url must be a string")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("integration base_url must be an absolute http or https URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("integration base_url must not include userinfo")

    def resolve_connector_target(self, url: str) -> tuple[Any, tuple[str, ...]]:
        """Resolve once, reject unsafe answers, and return addresses to pin for connection."""
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            raise ValueError("connector target must include a host")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            addresses = tuple(
                sorted(
                    {
                        str(ipaddress.ip_address(info[4][0]))
                        for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                    }
                )
            )
        except (OSError, ValueError) as exc:
            raise ValueError("connector target host could not be resolved") from exc
        if not addresses:
            raise ValueError("connector target host could not be resolved")
        if not self._allow_unsafe_connector_urls_for_tests and any(
            not ipaddress.ip_address(address).is_global for address in addresses
        ):
            raise ValueError("connector target resolves to a restricted network address")
        return parsed, addresses

    def register_integration_credential(
        self,
        integration_id: str,
        payload: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        secret = payload.get("secret")
        if not isinstance(secret, str) or not secret:
            raise ValueError("secret is required")
        integration = self.get_integration(integration_id)
        if integration["provider"] != "webhook" and not integration.get("base_url"):
            raise ValueError("integration base_url is required before registering a credential")
        timestamp = now_iso()
        with self._lock:
            updated = self.conn.execute(
                """
                UPDATE integrations
                SET secret_ref = ?, status = 'configured', credential_status = 'registered', updated_at = ?
                WHERE id = ?
                """,
                (token_ref(secret), timestamp, integration_id),
            ).rowcount
            if updated != 1:
                raise KeyError(integration_id)
            self._integration_secrets[integration_id] = secret
            self.conn.commit()
        self.audit("integration.credential.register", "integration", integration_id, request_id)
        return self.get_integration(integration_id)

    def list_integrations(self) -> list[dict[str, Any]]:
        rows = self.fetch_all("SELECT * FROM integrations ORDER BY created_at DESC")
        return [self.integration_from_row(row) for row in rows]

    def get_integration(self, integration_id: str) -> dict[str, Any]:
        row = self.fetch_one("SELECT * FROM integrations WHERE id = ?", (integration_id,))
        if row is None:
            raise KeyError(integration_id)
        return self.integration_from_row(row)

    def get_integration_secret_material(self, integration_id: str) -> str | None:
        self.get_integration(integration_id)
        return self._integration_secrets.get(integration_id)

    def integration_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        credential_registered = row["id"] in self._integration_secrets
        has_base_url = bool(row["base_url"])
        return {
            "id": row["id"],
            "type": "integration",
            "tenant_id": row["tenant_id"],
            "project_id": row["project_id"],
            "provider": row["provider"],
            "name": row["name"],
            "base_url": row["base_url"],
            "secret_ref": row["secret_ref"],
            "status": "configured" if credential_registered and (has_base_url or row["provider"] == "webhook") else (
                "credential_required" if has_base_url or row["provider"] == "webhook" else "metadata_only"
            ),
            "credential_status": "registered" if credential_registered else "registration_required",
            "capabilities": json_loads(row["capabilities"], {}),
            "metadata": json_loads(row["metadata"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def invoke_dify_chat(self, args: dict[str, Any]) -> dict[str, Any]:
        integration_id = args.get("integration_id")
        if not integration_id:
            raise ValueError("integration_id is required")
        integration = self.get_integration(integration_id)
        if integration["provider"] != "dify":
            raise ValueError("integration provider must be dify")
        query = args.get("query")
        if not query:
            raise ValueError("query is required")
        body = {
            "inputs": args.get("inputs", {}),
            "query": query,
            "response_mode": args.get("response_mode", "blocking"),
            "user": args.get("user") or "cloudagent-platform",
        }
        if args.get("conversation_id"):
            body["conversation_id"] = args["conversation_id"]
        return self.invoke_connector_json(
            integration,
            "chat-messages",
            body,
        )

    def invoke_feishu_message(self, args: dict[str, Any]) -> dict[str, Any]:
        integration_id = args.get("integration_id")
        if not integration_id:
            raise ValueError("integration_id is required")
        integration = self.get_integration(integration_id)
        if integration["provider"] != "feishu":
            raise ValueError("integration provider must be feishu")
        receive_id = args.get("receive_id")
        if not receive_id:
            raise ValueError("receive_id is required")
        msg_type = args.get("msg_type", "text")
        content = args.get("content")
        if content is None:
            raise ValueError("content is required")
        if isinstance(content, dict):
            content_value = json_dumps(content)
        elif msg_type == "text":
            content_value = json_dumps({"text": str(content)})
        else:
            content_value = str(content)
        return self.invoke_connector_json(
            integration,
            "open-apis/im/v1/messages",
            {"receive_id": receive_id, "msg_type": msg_type, "content": content_value},
            query={"receive_id_type": args.get("receive_id_type", "open_id")},
        )

    def invoke_connector_json(
        self,
        integration: dict[str, Any],
        path: str,
        body: dict[str, Any],
        query: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        base_url = integration.get("base_url")
        if not base_url:
            raise ValueError("integration base_url is required")
        self.validate_integration_base_url(base_url)
        token = self.get_integration_secret_material(integration["id"])
        if not token:
            raise PermissionError("integration token is not configured")
        url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
        if query:
            url = f"{url}?{urlencode(query)}"
        parsed_url, resolved_addresses = self.resolve_connector_target(url)
        raw_body = json_dumps(body).encode("utf-8")
        request_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "cloudagent-platform-connector/0.1",
            "Host": parsed_url.netloc,
        }
        request_path = parsed_url.path or "/"
        if parsed_url.query:
            request_path = f"{request_path}?{parsed_url.query}"
        port = parsed_url.port or (443 if parsed_url.scheme == "https" else 80)
        status_code: int | None = None
        response_body = ""
        last_transport_error: BaseException | None = None
        for resolved_address in resolved_addresses:
            if parsed_url.scheme == "https":
                connection: http.client.HTTPConnection = _PinnedHTTPSConnection(
                    parsed_url.hostname, port, resolved_address, timeout=30
                )
            else:
                connection = _PinnedHTTPConnection(parsed_url.hostname, port, resolved_address, timeout=30)
            try:
                connection.request("POST", request_path, body=raw_body, headers=request_headers)
                response = connection.getresponse()
                response_bytes = response.read(self.max_content_bytes + 1)
                status_code = response.status
                if len(response_bytes) > self.max_content_bytes:
                    raise ConnectorRequestError(status_code, "response body exceeds maximum size")
                response_body = response_bytes.decode("utf-8", errors="replace")
                break
            except ConnectorRequestError:
                raise
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                last_transport_error = exc
            finally:
                connection.close()
        if status_code is None:
            raise ConnectorRequestError(None) from last_transport_error
        if not 200 <= status_code < 300:
            raise ConnectorRequestError(status_code, redact_connector_response(response_body, token))
        try:
            parsed_body: Any = json.loads(response_body) if response_body else None
        except json.JSONDecodeError:
            parsed_body = response_body
        return {
            "provider": integration["provider"],
            "integration_id": integration["id"],
            "ok": True,
            "request": {
                "method": "POST",
                "url": url,
                "headers": {"Authorization": "Bearer <redacted>", "Content-Type": "application/json"},
                "json": body,
            },
            "response": {"status_code": status_code, "body": parsed_body},
        }

    def overview(self) -> dict[str, Any]:
        from .showcase import ShowcaseService

        return ShowcaseService(self).overview()

    def bootstrap_showcase(self, request_id: str) -> dict[str, Any]:
        from .showcase import ShowcaseService

        return ShowcaseService(self).bootstrap(request_id)



def parse_csv_set(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connect to a checked address while preserving the original Host header."""

    def __init__(self, host: str, port: int, resolved_address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._resolved_address = resolved_address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._resolved_address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS variant that pins TCP to a checked address but validates the hostname."""

    def __init__(self, host: str, port: int, resolved_address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._resolved_address = resolved_address

    def connect(self) -> None:
        raw_socket = socket.create_connection((self._resolved_address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)


def redact_connector_response(response_body: str, token: str) -> str:
    return response_body.replace(token, "<redacted>")[:1024]


def is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def run_server(
    host: str,
    port: int,
    database: str,
    auth_token: str,
    cors_origins: set[str] | None = None,
    max_json_bytes: int = DEFAULT_MAX_JSON_BYTES,
    max_content_bytes: int = DEFAULT_MAX_STORED_CONTENT_BYTES,
) -> ThreadingHTTPServer:
    runtime = Runtime(
        Store(database, max_content_bytes=max_content_bytes),
        auth_token,
        cors_origins=cors_origins,
        max_json_bytes=max_json_bytes,
    )
    server = ThreadingHTTPServer((host, port), make_handler(runtime))
    server.runtime = runtime  # type: ignore[attr-defined]
    print(f"CloudAgent-Platform listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("CloudAgent-Platform stopped.", flush=True)
    finally:
        runtime.stop()
        server.server_close()
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the CloudAgent-Platform local prototype.")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument(
        "--database",
        default=os.environ.get("CLOUDAGENT_DB", "cloudagent-platform.sqlite3"),
    )
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("CLOUDAGENT_AUTH_TOKEN", DEFAULT_TOKEN),
    )
    parser.add_argument(
        "--cors-origin",
        action="append",
        default=[],
        help="Allowed cross-origin caller. Can be repeated; same-origin requests do not need this.",
    )
    parser.add_argument(
        "--max-json-bytes",
        type=int,
        default=int(os.environ.get("CLOUDAGENT_MAX_JSON_BYTES", str(DEFAULT_MAX_JSON_BYTES))),
    )
    parser.add_argument(
        "--max-content-bytes",
        type=int,
        default=int(os.environ.get("CLOUDAGENT_MAX_CONTENT_BYTES", str(DEFAULT_MAX_STORED_CONTENT_BYTES))),
    )
    args = parser.parse_args()
    if not is_loopback_host(args.host) and args.auth_token == DEFAULT_TOKEN:
        parser.error("CLOUDAGENT_AUTH_TOKEN or --auth-token is required when binding outside localhost")
    cors_origins = parse_csv_set(os.environ.get("CLOUDAGENT_CORS_ORIGINS"))
    cors_origins.update(args.cors_origin)
    run_server(
        args.host,
        args.port,
        args.database,
        args.auth_token,
        cors_origins=cors_origins,
        max_json_bytes=args.max_json_bytes,
        max_content_bytes=args.max_content_bytes,
    )


if __name__ == "__main__":
    main()
