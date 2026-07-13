from __future__ import annotations

from copy import deepcopy
from typing import Any


POLICY_MODE_TO_DECISION = {
    "always_allow": "allow",
    "always_ask": "ask",
    "always_deny": "deny",
}


POLICY_MODE_RESTRICTIVENESS = {
    "always_allow": 0,
    "always_ask": 1,
    "always_deny": 2,
}


BASE_TOOL_POLICY_DEFAULTS = {
    "artifact.create": "always_allow",
    "file.read": "always_allow",
    "external.http": "always_ask",
    "integration.dify.chat": "always_ask",
    "integration.feishu.message": "always_ask",
    "shell.exec": "always_ask",
}


_PERMISSION_PROFILES: list[dict[str, Any]] = [
    {
        "id": "read-only",
        "type": "permission_profile",
        "name": "Read-only",
        "status": "available",
        "description": "Inspect resources and emit events without filesystem writes, shell execution, outbound network, or secret reads.",
        "filesystem": {
            "read": "workspace",
            "write": "none",
            "allow_host_mounts": False,
            "allow_docker_socket": False,
        },
        "network": {"mode": "deny_all", "allow_hosts": []},
        "secrets": {"mode": "none", "model_visible_plaintext": False},
        "tool_policy_defaults": {
            **BASE_TOOL_POLICY_DEFAULTS,
            "artifact.create": "always_ask",
            "external.http": "always_deny",
            "integration.dify.chat": "always_ask",
            "integration.feishu.message": "always_ask",
            "shell.exec": "always_deny",
        },
        "approval_policy": "on_request",
        "create_environment_allowed": True,
    },
    {
        "id": "workspace-write",
        "type": "permission_profile",
        "name": "Workspace write",
        "status": "available",
        "description": "Allow writes only inside the managed workspace while keeping network and secret access gated.",
        "filesystem": {
            "read": "workspace",
            "write": "workspace",
            "allow_host_mounts": False,
            "allow_docker_socket": False,
        },
        "network": {"mode": "deny_all", "allow_hosts": []},
        "secrets": {"mode": "brokered", "model_visible_plaintext": False},
        "tool_policy_defaults": BASE_TOOL_POLICY_DEFAULTS,
        "approval_policy": "on_request",
        "create_environment_allowed": True,
    },
    {
        "id": "network-limited",
        "type": "permission_profile",
        "name": "Network limited",
        "status": "available",
        "description": "Allow workspace writes and explicitly allowlisted public network egress; private networks and metadata IP remain denied.",
        "filesystem": {
            "read": "workspace",
            "write": "workspace",
            "allow_host_mounts": False,
            "allow_docker_socket": False,
        },
        "network": {
            "mode": "allowlist",
            "allow_hosts": [],
            "deny_private_networks": True,
            "deny_metadata_ip": True,
        },
        "secrets": {"mode": "brokered", "model_visible_plaintext": False},
        "tool_policy_defaults": BASE_TOOL_POLICY_DEFAULTS,
        "approval_policy": "on_request",
        "create_environment_allowed": True,
    },
    {
        "id": "danger-full-access",
        "type": "permission_profile",
        "name": "Danger full access",
        "status": "blocked",
        "description": "Documented for parity with local agent permission vocabularies, but intentionally unavailable in this local prototype.",
        "filesystem": {"read": "all", "write": "all"},
        "network": {"mode": "allow_all"},
        "secrets": {"mode": "plaintext_allowed"},
        "tool_policy_defaults": {
            "artifact.create": "always_allow",
            "file.read": "always_allow",
            "external.http": "always_allow",
            "integration.dify.chat": "always_allow",
            "integration.feishu.message": "always_allow",
            "shell.exec": "always_allow",
        },
        "approval_policy": "never",
        "create_environment_allowed": False,
    },
]


_SANDBOX_PROFILES: list[dict[str, Any]] = [
    {
        "id": "local-subprocess-deny-all",
        "type": "sandbox_profile",
        "name": "Local subprocess deny-all",
        "status": "implemented",
        "provider": "local-subprocess",
        "description": "Current local prototype provider: fixed Python subprocess, isolated temporary workspace, no shell expansion, no outbound network grant.",
        "isolation": {
            "kind": "subprocess",
            "production_grade": False,
            "chroot": False,
            "seccomp": False,
            "ephemeral_workspace": True,
            "workspace_cleanup": "delete_after_turn",
        },
        "network": {"mode": "deny_all"},
        "filesystem": {
            "root": "temporary_workspace",
            "read_only_root": True,
            "writable_paths": ["workspace"],
            "allow_host_mounts": False,
            "allow_docker_socket": False,
        },
        "create_environment_allowed": True,
    },
    {
        "id": "docker-deny-all",
        "type": "sandbox_profile",
        "name": "Docker deny-all",
        "status": "planned",
        "provider": "docker",
        "description": "P0 target shape for a single-host Docker sandbox with no privileged mode, no Docker socket, and default-deny network.",
        "isolation": {
            "kind": "container",
            "production_grade": False,
            "privileged": False,
            "allow_docker_socket": False,
            "ephemeral_workspace": True,
        },
        "network": {"mode": "deny_all", "deny_private_networks": True, "deny_metadata_ip": True},
        "filesystem": {
            "read_only_root": True,
            "writable_paths": ["/workspace"],
            "allow_host_mounts": False,
            "allow_docker_socket": False,
        },
        "create_environment_allowed": False,
    },
    {
        "id": "seccomp-chroot-network-gated",
        "type": "sandbox_profile",
        "name": "Seccomp chroot network-gated",
        "status": "reference_only",
        "provider": "external-sandbox-service",
        "description": "Reference profile inspired by Dify-style code execution isolation: chrooted filesystem, syscall filter, lowered privileges, and service-side network gate.",
        "isolation": {
            "kind": "sandbox_service",
            "production_grade": "depends_on_host_and_policy",
            "chroot": True,
            "seccomp": True,
            "uid_gid_drop": True,
        },
        "network": {"mode": "service_gate", "default_enabled": False},
        "filesystem": {"root": "sandbox_runtime_root", "cleanup": "provider_managed"},
        "create_environment_allowed": False,
    },
]


def list_permission_profiles() -> list[dict[str, Any]]:
    return deepcopy(_PERMISSION_PROFILES)


def get_permission_profile(profile_id: str) -> dict[str, Any]:
    for profile in _PERMISSION_PROFILES:
        if profile["id"] == profile_id:
            return deepcopy(profile)
    raise KeyError(profile_id)


def list_sandbox_profiles() -> list[dict[str, Any]]:
    return deepcopy(_SANDBOX_PROFILES)


def get_sandbox_profile(profile_id: str) -> dict[str, Any]:
    for profile in _SANDBOX_PROFILES:
        if profile["id"] == profile_id:
            return deepcopy(profile)
    raise KeyError(profile_id)


def validate_profile_for_environment(profile_id: str) -> dict[str, Any]:
    profile = get_permission_profile(profile_id)
    if not profile.get("create_environment_allowed"):
        raise ValueError(f"permission profile is not available for environments: {profile_id}")
    return profile


def validate_sandbox_for_environment(profile_id: str) -> dict[str, Any]:
    profile = get_sandbox_profile(profile_id)
    if not profile.get("create_environment_allowed"):
        raise ValueError(f"sandbox profile is not available for environments: {profile_id}")
    return profile


def environment_policy_defaults(
    permission_profile_id: str,
    sandbox_profile_id: str,
) -> dict[str, dict[str, Any]]:
    permission_profile = validate_profile_for_environment(permission_profile_id)
    sandbox_profile = validate_sandbox_for_environment(sandbox_profile_id)
    permission_network = permission_profile.get("network", {})
    sandbox_network = sandbox_profile.get("network", {})
    network_mode = (
        "deny_all"
        if "deny_all" in {permission_network.get("mode"), sandbox_network.get("mode")}
        else "allowlist"
    )
    network_policy: dict[str, Any] = {
        "mode": network_mode,
        "allow_hosts": [],
        "deny_private_networks": True,
        "deny_metadata_ip": True,
    }
    writable_paths = (
        []
        if permission_profile.get("filesystem", {}).get("write") == "none"
        else ["/workspace"]
    )
    return {
        "network_policy": network_policy,
        "filesystem_policy": {
            "workspace_root": "/workspace",
            "writable_paths": writable_paths,
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
    }


def validate_environment_policies(
    permission_profile_id: str,
    sandbox_profile_id: str,
    network_policy: dict[str, Any],
    filesystem_policy: dict[str, Any],
    secret_policy: dict[str, Any],
    package_policy: dict[str, Any],
) -> None:
    defaults = environment_policy_defaults(permission_profile_id, sandbox_profile_id)
    permission_profile = get_permission_profile(permission_profile_id)
    sandbox_profile = get_sandbox_profile(sandbox_profile_id)

    if not all(
        isinstance(policy, dict)
        for policy in (network_policy, filesystem_policy, secret_policy, package_policy)
    ):
        raise ValueError("environment policies must be objects")

    network_mode = network_policy.get("mode")
    allowed_network_modes = {"deny_all"}
    if (
        permission_profile.get("network", {}).get("mode") == "allowlist"
        and sandbox_profile.get("network", {}).get("mode") != "deny_all"
    ):
        allowed_network_modes.add("allowlist")
    if network_mode not in allowed_network_modes:
        raise ValueError(
            f"network policy is less restrictive than the selected profiles: {network_mode}"
        )
    allow_hosts = network_policy.get("allow_hosts", [])
    if not isinstance(allow_hosts, list) or not all(isinstance(host, str) for host in allow_hosts):
        raise ValueError("network_policy.allow_hosts must be a list of host names")
    if network_mode == "deny_all" and allow_hosts:
        raise ValueError("deny_all network policy cannot include allow_hosts")
    if network_policy.get("deny_private_networks", True) is not True:
        raise ValueError("network policy must deny private networks")
    if network_policy.get("deny_metadata_ip", True) is not True:
        raise ValueError("network policy must deny metadata IP access")

    if filesystem_policy.get("workspace_root", "/workspace") != "/workspace":
        raise ValueError("filesystem workspace_root must be /workspace")
    if filesystem_policy.get("read_only_root", True) is not True:
        raise ValueError("filesystem root must remain read-only")
    if filesystem_policy.get("allow_host_mounts", False) is not False:
        raise ValueError("host mounts are not available in this prototype")
    if filesystem_policy.get("allow_docker_socket", False) is not False:
        raise ValueError("Docker socket access is not available in this prototype")
    writable_paths = filesystem_policy.get(
        "writable_paths",
        defaults["filesystem_policy"]["writable_paths"],
    )
    if not isinstance(writable_paths, list) or not all(
        isinstance(path, str) for path in writable_paths
    ):
        raise ValueError("filesystem_policy.writable_paths must be a list of paths")
    if permission_profile.get("filesystem", {}).get("write") == "none" and writable_paths:
        raise ValueError("read-only permission profile cannot include writable paths")
    for path in writable_paths:
        parts = [part for part in path.split("/") if part]
        if not path.startswith("/") or not parts or parts[0] != "workspace" or ".." in parts:
            raise ValueError(f"writable path must stay inside /workspace: {path}")

    if secret_policy.get("mode", "none") != "none":
        raise ValueError("runtime secret injection is not implemented in this prototype")
    if secret_policy.get("allow_runtime_injection", False) is not False:
        raise ValueError("runtime secret injection is not implemented in this prototype")
    if secret_policy.get("allow_model_visible_plaintext", False) is not False:
        raise ValueError("model-visible plaintext secrets are not allowed")

    if package_policy.get("mode", "none") != "none":
        raise ValueError("runtime package installation is not implemented in this prototype")
    if package_policy.get("allow_runtime_install", False) is not False:
        raise ValueError("runtime package installation is not implemented in this prototype")


def policy_mode_to_decision(mode: str) -> str:
    try:
        return POLICY_MODE_TO_DECISION[mode]
    except KeyError as exc:
        raise ValueError(f"unknown policy mode: {mode}") from exc


def more_restrictive_policy_mode(left: str, right: str) -> str:
    if left not in POLICY_MODE_RESTRICTIVENESS:
        raise ValueError(f"unknown policy mode: {left}")
    if right not in POLICY_MODE_RESTRICTIVENESS:
        raise ValueError(f"unknown policy mode: {right}")
    if POLICY_MODE_RESTRICTIVENESS[left] >= POLICY_MODE_RESTRICTIVENESS[right]:
        return left
    return right


def profile_tool_defaults(profile_id: str) -> dict[str, str]:
    profile = get_permission_profile(profile_id)
    return dict(profile.get("tool_policy_defaults", {}))


def merge_tool_policy_defaults(
    profile_id: str,
    environment_defaults: dict[str, Any] | None,
) -> dict[str, str]:
    defaults = profile_tool_defaults(profile_id)
    for tool, mode in (environment_defaults or {}).items():
        if mode not in POLICY_MODE_TO_DECISION:
            raise ValueError(f"invalid tool policy mode for {tool}: {mode}")
        base_mode = defaults.get(str(tool), "always_allow")
        if POLICY_MODE_RESTRICTIVENESS[str(mode)] < POLICY_MODE_RESTRICTIVENESS[base_mode]:
            raise ValueError(
                f"tool policy override cannot be less restrictive than permission profile for {tool}: "
                f"{base_mode} -> {mode}"
            )
        defaults[str(tool)] = str(mode)
    return defaults
