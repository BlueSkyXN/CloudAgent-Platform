from __future__ import annotations


def sdlc_status_payload() -> dict[str, object]:
    return {
        "type": "cloudagent.sdlc.status",
        "status": "local-prototype-active",
        "generated_package": "local/20260616",
        "implemented_scope": "local-control-plane-prototype",
        "runtime_capabilities": {
            "event_store": True,
            "scheduler_delay_trigger": True,
            "tool_gateway": True,
            "permission_profiles": True,
            "sandbox_profiles": True,
            "vaults": True,
            "vault_secret_storage": "digest_only",
            "vault_runtime_injection": False,
            "local_subprocess_adapter": True,
            "codex_cli_probe": True,
            "production_sandbox": False,
            "full_cluster_runtime": False,
        },
        "notes": [
            "This local service exposes the current implemented prototype contract.",
            "The full CloudAgent cluster runtime is still in progress.",
        ],
    }
