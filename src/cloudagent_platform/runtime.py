from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol


class RuntimeStore(Protocol):
    def append_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        request_id: str,
        severity: str = "info",
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        ...

    def create_artifact(
        self,
        session_id: str,
        payload: dict[str, Any],
        request_id: str,
        turn_id: str | None = None,
        emit_event: bool = True,
    ) -> dict[str, Any]:
        ...

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
        ...


@dataclass(frozen=True)
class RuntimeContext:
    session_id: str
    turn_id: str
    source: str
    request_id: str
    adapter_id: str
    kernel_id: str
    run_id: str | None = None
    worker_id: str | None = None


class RuntimeAdapter(Protocol):
    adapter_id: str
    kernel_id: str

    def manifest(self) -> dict[str, Any]:
        ...

    def execute_turn(self, store: RuntimeStore, context: RuntimeContext) -> dict[str, Any]:
        ...


ProbeRunner = Callable[[list[str], int], subprocess.CompletedProcess[str]]
Which = Callable[[str], str | None]


@dataclass(frozen=True)
class SandboxResult:
    sandbox_id: str
    provider: str
    status: str
    returncode: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    workspace_deleted: bool
    created_files: list[str]


class SandboxProvider(Protocol):
    provider_id: str

    def manifest(self) -> dict[str, Any]:
        ...

    def run(self, context: RuntimeContext, payload: dict[str, Any]) -> SandboxResult:
        ...


class LocalSubprocessSandboxProvider:
    provider_id = "local-subprocess"

    def __init__(self, timeout_seconds: int = 5) -> None:
        self.timeout_seconds = timeout_seconds

    def manifest(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "isolation": "local-subprocess",
            "timeout_seconds": self.timeout_seconds,
            "network_policy": "not_granted_to_adapter_payload",
            "filesystem_policy": "temporary_workspace_deleted_after_turn",
            "shell": False,
            "note": "Prototype provider runs a fixed Python worker in a temporary directory; it is not a production sandbox.",
        }

    def run(self, context: RuntimeContext, payload: dict[str, Any]) -> SandboxResult:
        sandbox_id = f"sbx_{context.turn_id}"
        workspace = Path(tempfile.mkdtemp(prefix="cloudagent-sandbox-"))
        started = time.monotonic()
        timed_out = False
        completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        try:
            (workspace / "input.json").write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            script = (
                "import json, pathlib\n"
                "workspace = pathlib.Path.cwd()\n"
                "payload = json.loads((workspace / 'input.json').read_text(encoding='utf-8'))\n"
                "summary = {\n"
                "  'adapter_id': payload['adapter_id'],\n"
                "  'kernel_id': payload['kernel_id'],\n"
                "  'session_id': payload['session_id'],\n"
                "  'turn_id': payload['turn_id'],\n"
                "  'source': payload['source'],\n"
                "  'sandbox': 'local-subprocess',\n"
                "}\n"
                "(workspace / 'summary.json').write_text(json.dumps(summary, sort_keys=True), encoding='utf-8')\n"
                "print(json.dumps({'status': 'ok', 'summary_file': 'summary.json'}, sort_keys=True))\n"
            )
            completed = subprocess.run(
                [sys.executable, "-I", "-c", script],
                cwd=workspace,
                env={"PYTHONIOENCODING": "utf-8"},
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            completed = subprocess.CompletedProcess(
                args=exc.cmd if isinstance(exc.cmd, list) else [],
                returncode=None,
                stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
                stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "sandbox timed out",
            )
        finally:
            created_files = sorted(path.name for path in workspace.iterdir()) if workspace.exists() else []
            shutil.rmtree(workspace, ignore_errors=True)
        duration_ms = int((time.monotonic() - started) * 1000)
        return SandboxResult(
            sandbox_id=sandbox_id,
            provider=self.provider_id,
            status="timeout" if timed_out else ("succeeded" if completed.returncode == 0 else "failed"),
            returncode=completed.returncode,
            stdout=completed.stdout[-4000:],
            stderr=completed.stderr[-4000:],
            duration_ms=duration_ms,
            timed_out=timed_out,
            workspace_deleted=not workspace.exists(),
            created_files=created_files,
        )


class LocalPrototypeAdapter:
    adapter_id = "local-prototype-adapter"
    kernel_id = "codex-cli-local"

    def __init__(self, sandbox: SandboxProvider | None = None) -> None:
        self.sandbox = sandbox or LocalSubprocessSandboxProvider()

    def manifest(self) -> dict[str, Any]:
        sandbox_manifest = self.sandbox.manifest()
        return {
            "adapter_id": self.adapter_id,
            "kernel_id": self.kernel_id,
            "runtime_mode": "kernel_in_sandbox",
            "sandbox_provider": sandbox_manifest["provider_id"],
            "capabilities": {
                "streaming": True,
                "multi_turn": True,
                "tool_calling": True,
                "file_edit": False,
                "shell_exec": False,
                "approval": True,
                "resume": False,
                "artifact": True,
            },
            "constraints": {
                "network": "deny_all",
                "filesystem": "temporary_workspace",
                "sandbox": sandbox_manifest,
                "note": "Prototype adapter executes a fixed local subprocess worker; it does not run arbitrary shell commands.",
            },
        }

    def execute_turn(self, store: RuntimeStore, context: RuntimeContext) -> dict[str, Any]:
        store.append_event(
            context.session_id,
            "worker.assigned",
            {"run_id": context.run_id, "worker_id": context.worker_id, "source": context.source},
            context.request_id,
            turn_id=context.turn_id,
        )
        store.append_event(
            context.session_id,
            "kernel.started",
            {
                "adapter_id": context.adapter_id,
                "kernel_id": context.kernel_id,
                "run_id": context.run_id,
            },
            context.request_id,
            turn_id=context.turn_id,
        )
        store.append_event(
            context.session_id,
            "sandbox.created",
            {
                "provider": self.sandbox.provider_id,
                "network_policy": "deny_all",
                "filesystem_policy": "temporary_workspace_deleted_after_turn",
                "timeout_seconds": self.sandbox.manifest()["timeout_seconds"],
            },
            context.request_id,
            turn_id=context.turn_id,
        )
        sandbox_result = self.sandbox.run(
            context,
            {
                "adapter_id": context.adapter_id,
                "kernel_id": context.kernel_id,
                "session_id": context.session_id,
                "turn_id": context.turn_id,
                "source": context.source,
                "run_id": context.run_id,
                "worker_id": context.worker_id,
            },
        )
        store.append_event(
            context.session_id,
            "sandbox.stdout",
            {
                "sandbox_id": sandbox_result.sandbox_id,
                "stdout": sandbox_result.stdout,
                "stderr": sandbox_result.stderr,
                "returncode": sandbox_result.returncode,
            },
            context.request_id,
            severity="error" if sandbox_result.status != "succeeded" else "info",
            turn_id=context.turn_id,
        )
        store.append_event(
            context.session_id,
            "agent.message",
            {
                "text": "Local prototype adapter executed a fixed subprocess sandbox turn. Attach a real Codex CLI adapter for model-driven work.",
                "source": context.source,
                "adapter_id": context.adapter_id,
                "sandbox_status": sandbox_result.status,
            },
            context.request_id,
            turn_id=context.turn_id,
        )
        artifact = store.create_artifact(
            context.session_id,
            {
                "name": "local-turn-summary.json",
                "content_type": "application/json",
                "content": json.dumps(
                    {
                        "session_id": context.session_id,
                        "turn_id": context.turn_id,
                        "source": context.source,
                        "adapter_id": context.adapter_id,
                        "sandbox_id": sandbox_result.sandbox_id,
                        "sandbox_status": sandbox_result.status,
                        "sandbox_stdout": sandbox_result.stdout,
                        "workspace_deleted": sandbox_result.workspace_deleted,
                        "runtime": "local-prototype",
                    },
                    sort_keys=True,
                ),
            },
            context.request_id,
            turn_id=context.turn_id,
        )
        usage = store.record_usage(
            context.session_id,
            context.turn_id,
            context.request_id,
            run_id=context.run_id,
            worker_id=context.worker_id,
            token_input=0,
            token_output=0,
            tool_duration_ms=sandbox_result.duration_ms,
            sandbox_cpu_ms=sandbox_result.duration_ms,
            sandbox_disk_read_bytes=0,
            sandbox_disk_write_bytes=0,
            sandbox_network_bytes=0,
        )
        store.append_event(
            context.session_id,
            "sandbox.cleanup",
            {
                "provider": self.sandbox.provider_id,
                "sandbox_id": sandbox_result.sandbox_id,
                "status": "completed" if sandbox_result.workspace_deleted else "failed",
                "workspace_deleted": sandbox_result.workspace_deleted,
                "created_files": sandbox_result.created_files,
            },
            context.request_id,
            turn_id=context.turn_id,
        )
        store.append_event(
            context.session_id,
            "kernel.completed",
            {
                "adapter_id": context.adapter_id,
                "kernel_id": context.kernel_id,
                "artifact_id": artifact["id"],
                "usage_id": usage["id"],
                "sandbox_id": sandbox_result.sandbox_id,
                "sandbox_status": sandbox_result.status,
            },
            context.request_id,
            severity="error" if sandbox_result.status != "succeeded" else "info",
            turn_id=context.turn_id,
        )
        return {
            "adapter_id": context.adapter_id,
            "kernel_id": context.kernel_id,
            "artifact_id": artifact["id"],
            "usage_id": usage["id"],
            "sandbox_id": sandbox_result.sandbox_id,
            "sandbox": {
                "provider": sandbox_result.provider,
                "status": sandbox_result.status,
                "returncode": sandbox_result.returncode,
                "duration_ms": sandbox_result.duration_ms,
                "workspace_deleted": sandbox_result.workspace_deleted,
            },
            "status": sandbox_result.status,
        }


class CodexCliProbeAdapter:
    adapter_id = "codex-cli-probe-adapter"
    kernel_id = "codex-cli-probe"

    def __init__(
        self,
        binary_name: str = "codex",
        timeout_seconds: int = 3,
        runner: ProbeRunner | None = None,
        which: Which | None = None,
    ) -> None:
        self.binary_name = binary_name
        self.timeout_seconds = timeout_seconds
        self._runner = runner or self._run_command
        self._which = which or shutil.which

    def manifest(self) -> dict[str, Any]:
        probe = self.probe()
        return {
            "adapter_id": self.adapter_id,
            "kernel_id": self.kernel_id,
            "runtime_mode": "kernel_probe",
            "sandbox_provider": None,
            "capabilities": {
                "streaming": False,
                "multi_turn": False,
                "tool_calling": False,
                "file_edit": False,
                "shell_exec": False,
                "approval": False,
                "resume": False,
                "artifact": False,
                "probe": True,
            },
            "constraints": {
                "network": "not_used",
                "filesystem": "not_used",
                "note": "Probe-only adapter runs codex --version and never executes prompts or agent tasks.",
            },
            "probe": probe,
        }

    def probe(self) -> dict[str, Any]:
        probed_at = datetime.now(timezone.utc).isoformat()
        binary = self._which(self.binary_name)
        if not binary:
            return {
                "available": False,
                "binary": None,
                "version": None,
                "probe_status": "missing_binary",
                "checked_at": probed_at,
                "timeout_seconds": self.timeout_seconds,
            }
        try:
            completed = self._runner([binary, "--version"], self.timeout_seconds)
        except subprocess.TimeoutExpired:
            return {
                "available": False,
                "binary": binary,
                "version": None,
                "probe_status": "timeout",
                "checked_at": probed_at,
                "timeout_seconds": self.timeout_seconds,
            }
        except OSError as exc:
            return {
                "available": False,
                "binary": binary,
                "version": None,
                "probe_status": "error",
                "error": exc.__class__.__name__,
                "checked_at": probed_at,
                "timeout_seconds": self.timeout_seconds,
            }

        output = (completed.stdout or completed.stderr or "").strip().splitlines()
        version = output[0][:200] if output else None
        return {
            "available": completed.returncode == 0,
            "binary": binary,
            "version": version,
            "probe_status": "ok" if completed.returncode == 0 else "nonzero_exit",
            "returncode": completed.returncode,
            "checked_at": probed_at,
            "timeout_seconds": self.timeout_seconds,
        }

    def execute_turn(self, store: RuntimeStore, context: RuntimeContext) -> dict[str, Any]:
        probe = self.probe()
        store.append_event(
            context.session_id,
            "kernel.probed",
            {
                "adapter_id": self.adapter_id,
                "kernel_id": self.kernel_id,
                "probe": probe,
                "dry_run": True,
            },
            context.request_id,
            turn_id=context.turn_id,
        )
        return {
            "adapter_id": self.adapter_id,
            "kernel_id": self.kernel_id,
            "status": "succeeded" if probe["available"] else "degraded",
            "probe": probe,
        }

    @staticmethod
    def _run_command(command: list[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
