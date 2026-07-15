from __future__ import annotations


def sdlc_status_payload() -> dict[str, object]:
    return {
        "type": "cloudagent.sdlc.status",
        "status": "company-showcase-release",
        "generated_package": "local/20260616",
        "implemented_scope": "governed-local-control-plane-and-showcase-console",
        "runtime_capabilities": {
            "showcase_console": True,
            "showcase_bootstrap": True,
            "event_store": True,
            "scheduler_delay_trigger": True,
            "tool_gateway": True,
            "permission_profiles": True,
            "sandbox_profiles": True,
            "vaults": True,
            "vault_secret_storage": "digest_only",
            "vault_runtime_injection": False,
            "integration_secret_storage": "process_memory_only",
            "integration_secret_reregistration_after_restart": True,
            "session_dispatch": "single_running_run_per_session_per_process",
            "tool_execution_binding": "action_run_lease_generation",
            "local_subprocess_adapter": True,
            "codex_cli_probe": True,
            "production_sandbox": False,
            "full_cluster_runtime": False,
        },
        "notes": [
            "The company-showcase console uses the same protected APIs and persisted runtime evidence as external clients.",
            "Integration credentials are process-memory only and must be re-registered after a service restart.",
            "Production sandbox isolation, runtime Vault injection, and a hardened distributed worker fleet remain out of scope.",
        ],
    }
