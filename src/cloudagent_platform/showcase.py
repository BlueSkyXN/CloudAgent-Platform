from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import __version__

if TYPE_CHECKING:
    from .app import Store


SHOWCASE_MARKER = "bootstrap-v1"
SHOWCASE_ENVIRONMENT_NAME = "Showcase Workspace"
SHOWCASE_WORKER_ID = "showcase-worker"


class ShowcaseService:
    """Derived local-showcase views and idempotent demonstration resources."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def overview(self) -> dict[str, Any]:
        counts = self._counts()
        signals = {
            "queue_depth": self._count_where("job_runs", "status = 'queued'"),
            "active_workers": self._count_where(
                "workers", "status = 'active' AND active_run_id IS NULL"
            ),
            "pending_approvals": self._count_where("pending_actions", "status = 'pending'"),
            "completed_runs": self._count_where("job_runs", "status = 'succeeded'"),
        }
        permission_profiles = self.store.list_permission_profiles()
        sandbox_profiles = self.store.list_sandbox_profiles()
        active_local_sandbox = next(
            (item for item in sandbox_profiles if item["id"] == "local-subprocess-deny-all"),
            None,
        )
        artifacts_supported = bool(
            self.store.adapter.manifest().get("capabilities", {}).get("artifact")
        )
        policy_ready = any(item.get("status") == "available" for item in permission_profiles)
        sandbox_ready = bool(
            active_local_sandbox and active_local_sandbox.get("status") == "implemented"
        )
        rail = [
            self._rail_stage(
                "api_gateway",
                "ready",
                "Authenticated HTTP API is available in the local prototype.",
            ),
            self._rail_stage(
                "policy",
                "ready" if policy_ready and sandbox_ready else "attention",
                "Permission and sandbox profiles are evaluated when environments are created.",
            ),
            self._rail_stage(
                "queue",
                "ready",
                f"{signals['queue_depth']} queued run(s) persisted in SQLite.",
            ),
            self._rail_stage(
                "worker",
                "ready" if signals["active_workers"] else "waiting",
                (
                    f"{signals['active_workers']} active worker(s) can claim queued runs."
                    if signals["active_workers"]
                    else "No active worker is currently registered; queued runs remain claimable."
                ),
            ),
            self._rail_stage(
                "artifact_audit",
                "ready" if artifacts_supported else "attention",
                "Artifacts and audit records are persisted locally; lease tokens are never included in this view.",
            ),
        ]
        mandatory_ready = all(stage["status"] == "ready" for stage in rail if stage["id"] != "worker")
        readiness_status = "ready" if mandatory_ready else "attention"
        return {
            # Existing compatibility fields.
            "type": "cloudagent.admin.overview",
            "service": "CloudAgent-Platform",
            "version": __version__,
            "counts": counts,
            "permission_profiles": permission_profiles,
            "sandbox_profiles": sandbox_profiles,
            "tools": self.store.list_tools(),
            "recent_sessions": self.store.list_sessions()[:5],
            "recent_jobs": self.store.list_jobs()[:5],
            "recent_runs": self.store.list_runs()[:5],
            "recent_artifacts": self.store.list_artifacts()[:5],
            "vaults": self.store.list_vaults()[:5],
            "pending_actions": self.store.list_pending_actions()[:5],
            "workers": self.store.list_workers(),
            "integrations": self.store.list_integrations(),
            # Showcase-specific fields are derived from the same persisted resources.
            "readiness": {
                "overall": readiness_status,
                "status": "local_showcase",
                "summary": (
                    "Local control-plane capabilities are ready for demonstration. "
                    "This is not a production sandbox or a vault-injection runtime."
                    if readiness_status == "ready"
                    else "The local control-plane has a capability requiring attention."
                ),
            },
            "runtime_rail": rail,
            "signals": signals,
            "activity": {"items": self._recent_activity()},
            "capability_boundary": {
                "local_showcase": True,
                "production_sandbox": False,
                "vault_injection": False,
                "summary": (
                    "Uses the local prototype adapter and local SQLite only. "
                    "It does not claim production-grade sandbox isolation or runtime vault injection."
                ),
            },
        }

    def session_workspace(self, session_id: str) -> dict[str, Any]:
        """Return the complete read model used by the Console session workspace."""
        # The Store lock is re-entrant, so all component reads describe one
        # coherent local control-plane snapshot without weakening their APIs.
        with self.store._lock:
            session = self.store.get_session(session_id)
            events = self.store.list_events(session_id)
            artifacts = self.store.list_artifacts(session_id)
            usage = self.store.list_usage(session_id)
            pending_actions = self.store.list_pending_actions(session_id)
            audit = self.store.list_session_audit(session_id)
            tools = self.store.list_tools()
        return {
            "type": "cloudagent.admin.session_workspace",
            "session": session,
            "events": events,
            "artifacts": artifacts,
            "usage": usage,
            "pending_actions": pending_actions,
            "audit": audit,
            "tools": tools,
            "counts": {
                "events": len(events),
                "artifacts": len(artifacts),
                "usage_records": len(usage),
                "pending_actions": len(pending_actions),
                "audit_records": len(audit),
            },
            "last_event_id": events[-1]["id"] if events else None,
        }

    def bootstrap(self, request_id: str) -> dict[str, Any]:
        # Store operations use the same re-entrant lock, so this covers every
        # list/create decision as one local-process idempotency boundary.
        with self.store._lock:
            return self._bootstrap_locked(request_id)

    def _bootstrap_locked(self, request_id: str) -> dict[str, Any]:
        """Create one coherent, reusable local demonstration topology."""
        created: list[str] = []
        reused: list[str] = []

        agent = self._find_marked(self.store.list_agents(), "agent")
        if agent is None:
            agent = self.store.create_agent(
                {
                    "name": "Showcase Operations Agent",
                    "description": "Local showcase agent for the CloudAgent control plane.",
                    "metadata": {"showcase": SHOWCASE_MARKER, "role": "agent"},
                },
                request_id,
            )
            created.append("agent")
        else:
            reused.append("agent")

        environment = next(
            (
                item
                for item in self.store.list_environments()
                if item["name"] == SHOWCASE_ENVIRONMENT_NAME
                and item["permission_profile_id"] == "workspace-write"
                and item["sandbox_profile_id"] == "local-subprocess-deny-all"
            ),
            None,
        )
        if environment is None:
            environment = self.store.create_environment(
                {
                    "name": SHOWCASE_ENVIRONMENT_NAME,
                    "runtime_type": "local-subprocess",
                    "permission_profile_id": "workspace-write",
                    "sandbox_profile_id": "local-subprocess-deny-all",
                },
                request_id,
            )
            created.append("environment")
        else:
            reused.append("environment")

        session = next(
            (
                item
                for item in self.store.list_sessions()
                if self._has_marker(item, "session")
                and item["agent_snapshot"]["id"] == agent["id"]
                and item["environment_snapshot"]["id"] == environment["id"]
            ),
            None,
        )
        if session is None:
            session = self.store.create_session(
                {
                    "agent_id": agent["id"],
                    "environment_id": environment["id"],
                    "metadata": {"showcase": SHOWCASE_MARKER, "role": "session"},
                },
                request_id,
            )
            created.append("session")
        else:
            reused.append("session")

        job = next(
            (
                item
                for item in self.store.list_jobs()
                if self._has_marker(item, "job")
                and item["agent_id"] == agent["id"]
                and item["environment_id"] == environment["id"]
            ),
            None,
        )
        if job is None:
            job = self.store.create_job(
                {
                    "name": "Showcase Manual Job",
                    "agent_id": agent["id"],
                    "environment_id": environment["id"],
                    "trigger": {"type": "manual"},
                    "metadata": {"showcase": SHOWCASE_MARKER, "role": "job"},
                },
                request_id,
            )
            created.append("job")
        else:
            reused.append("job")

        worker = next(
            (item for item in self.store.list_workers() if item["id"] == SHOWCASE_WORKER_ID),
            None,
        )
        if worker is None:
            worker = self.store.register_worker(
                {
                    "id": SHOWCASE_WORKER_ID,
                    "name": "Showcase Worker",
                    "capabilities": {
                        "local_noop_turn": True,
                        "session_events": True,
                        "showcase": True,
                    },
                },
                request_id,
            )
            created.append("worker")
        else:
            if worker["status"] != "active" and not worker.get("active_run_id"):
                worker = self.store.register_worker(
                    {
                        "id": SHOWCASE_WORKER_ID,
                        "name": "Showcase Worker",
                        "status": "active",
                        "capabilities": worker.get("capabilities", {}),
                    },
                    request_id,
                )
            reused.append("worker")

        run_row = self.store.fetch_one(
            "SELECT * FROM job_runs WHERE job_id = ? AND session_id = ? ORDER BY created_at DESC LIMIT 1",
            (job["id"], session["id"]),
        )
        if run_row is None:
            run = self.store.create_run(
                session["id"],
                request_id,
                trigger_source="showcase:bootstrap",
                job_id=job["id"],
                assign_worker=False,
            )
            created.append("run")
        else:
            run = self.store.run_from_row(run_row)
            reused.append("run")

        return {
            "type": "cloudagent.admin.showcase_bootstrap",
            "agent": agent,
            "environment": environment,
            "session": session,
            "job": job,
            "worker": worker,
            "run": run,
            "created": created,
            "reused": reused,
        }

    def _counts(self) -> dict[str, int]:
        return {
            table: self._count_where(table, "1 = 1")
            for table in (
                "agents",
                "environments",
                "sessions",
                "events",
                "jobs",
                "job_runs",
                "workers",
                "files",
                "artifacts",
                "tool_policies",
                "pending_actions",
                "usage_records",
                "vaults",
                "vault_credentials",
                "integrations",
            )
        }

    def _count_where(self, table: str, predicate: str) -> int:
        row = self.store.fetch_one(
            f"SELECT COUNT(*) AS count FROM {table} WHERE {predicate}", ()
        )
        return int(row["count"]) if row else 0

    @staticmethod
    def _rail_stage(stage_id: str, status: str, summary: str) -> dict[str, str]:
        return {"id": stage_id, "status": status, "summary": summary}

    def _recent_activity(self, limit: int = 12) -> list[dict[str, Any]]:
        rows = self.store.fetch_all(
            """
            SELECT id, created_at, 'event' AS kind, type AS action, severity, session_id AS subject_id
            FROM events
            UNION ALL
            SELECT id, created_at, 'audit' AS kind, action, NULL AS severity, target_id AS subject_id
            FROM audit_log
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            {
                "id": row["id"],
                "kind": row["kind"],
                "action": row["action"],
                "severity": row["severity"],
                "subject_id": row["subject_id"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    @staticmethod
    def _has_marker(item: dict[str, Any], role: str) -> bool:
        metadata = item.get("metadata")
        return (
            isinstance(metadata, dict)
            and metadata.get("showcase") == SHOWCASE_MARKER
            and metadata.get("role") == role
        )

    def _find_marked(self, resources: list[dict[str, Any]], role: str) -> dict[str, Any] | None:
        return next((item for item in resources if self._has_marker(item, role)), None)
