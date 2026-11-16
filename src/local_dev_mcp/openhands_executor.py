"""OpenHands executor adapter for the MCP Tool Bridge.

Implements the ``ExecutorPort`` contract against a pinned, hardened OpenHands
agent-server container image. The adapter:

- pins the container image by digest (unpinned images are rejected by default);
- builds fixed argv vectors for every docker invocation — never a shell;
- binds the container to the loopback host only, with all capabilities dropped,
  ``no-new-privileges`` enforced, and an ephemeral session API key;
- mounts only the candidate worktree (read-write) at ``/workspace`` — the
  canonical checkout and host credentials are never mounted;
- guarantees container teardown via ``try ... finally`` on every exit path,
  including crashes, timeouts, and cancellation;
- always returns results with ``grants_authority=False`` — executor output is
  evidence only.

Zero framework imports; stdlib only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from typing import Any, Callable, Protocol, runtime_checkable
import urllib.error
import urllib.request

from .executor_contract import (
    ExecutionRequest,
    ExecutionResult,
    ExecutorCapabilities,
    ExecutorCheckpoint,
    ExecutorHealthStatus,
    ExecutorLifecycleState,
    ExecutorPort,
    ExecutorResourceUsage,
    NetworkMode,
)


PINNED_OPENHANDS_IMAGE = (
    "ghcr.io/openhands/agent-server@sha256:"
    "67d3b88984dd0537de78cf5a354898942d601b8fab9b633a655a4d57bed08d02"
)

_REDACTED = "<redacted>"

ADAPTER_ID = "openhands"
ADAPTER_VERSION = "1.0.0"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _scrub_secrets(text: str, secrets_: Sequence[str]) -> str:
    """Replace any occurrence of the given secret values inside ``text``."""
    for secret in secrets_:
        if secret:
            text = text.replace(secret, _REDACTED)
    return text


def _key_is_sensitive(key: Any) -> bool:
    """Return True for dict keys that must never appear in diagnostics."""
    if not isinstance(key, str):
        return False
    lowered = key.strip().lower()
    return (
        lowered == "session_api_key"
        or lowered == "x-session-api-key"
        or lowered.startswith("oh_session_api_keys_")
    )


def redact_openhands_diagnostics(data: Any, *, secrets: Sequence[str] = ()) -> Any:
    """Recursively scrub OpenHands diagnostics of session-key material.

    Any mapping key named ``session_api_key``, matching the
    ``OH_SESSION_API_KEYS_*`` env-key pattern, or matching the
    ``X-Session-API-Key`` HTTP header is replaced with ``<redacted>``.
    Additionally, any value that exactly equals one of the provided secret
    strings is replaced with ``<redacted>``.
    """
    secret_values = {s for s in secrets if isinstance(s, str) and s}

    def _redact(value: Any) -> Any:
        if isinstance(value, str):
            return _REDACTED if value in secret_values else value
        if isinstance(value, Mapping):
            redacted: dict[Any, Any] = {}
            for key, item in value.items():
                if _key_is_sensitive(key):
                    redacted[key] = _REDACTED
                else:
                    redacted[key] = _redact(item)
            return redacted
        if isinstance(value, tuple):
            return tuple(_redact(item) for item in value)
        if isinstance(value, list):
            return [_redact(item) for item in value]
        return value

    return _redact(data)


@dataclass(frozen=True)
class OpenHandsAdapterConfig:
    """Configuration for the OpenHands container adapter.

    The image must be digest-pinned (``@sha256:...``) unless
    ``allow_unpinned_image`` is explicitly set, and the published port is
    bound to the loopback host only.
    """

    image: str = PINNED_OPENHANDS_IMAGE
    docker_binary: str = "docker"
    loopback_host: str = "127.0.0.1"
    container_port: int = 8000
    network_mode: NetworkMode = NetworkMode.NONE
    startup_timeout_seconds: float = 30.0
    execution_timeout_seconds: float = 300.0
    allow_unpinned_image: bool = False
    host_uid: int | None = None
    host_gid: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.image, str) or not self.image.strip():
            raise ValueError("image must be a non-empty string")
        if not self.allow_unpinned_image and "@sha256:" not in self.image:
            raise ValueError(
                "OpenHands image must be digest-pinned (@sha256:...); "
                "refusing unpinned image "
                f"{self.image!r} (set allow_unpinned_image=True to override)"
            )
        if self.loopback_host not in ("127.0.0.1", "localhost"):
            raise ValueError(
                f"loopback_host must be '127.0.0.1' or 'localhost', got {self.loopback_host!r}"
            )
        if (
            isinstance(self.startup_timeout_seconds, bool)
            or not isinstance(self.startup_timeout_seconds, (int, float))
            or self.startup_timeout_seconds <= 0
        ):
            raise ValueError("startup_timeout_seconds must be positive")
        if (
            isinstance(self.execution_timeout_seconds, bool)
            or not isinstance(self.execution_timeout_seconds, (int, float))
            or self.execution_timeout_seconds <= 0
        ):
            raise ValueError("execution_timeout_seconds must be positive")
        if isinstance(self.network_mode, str):
            object.__setattr__(self, "network_mode", NetworkMode(self.network_mode))


def build_openhands_container_argv(
    config: OpenHandsAdapterConfig,
    *,
    container_name: str,
    candidate_root: Path,
    session_api_key: str,
    extra_env: Mapping[str, str] | None = None,
) -> list[str]:
    """Build the fixed docker argv vector for one OpenHands container.

    The vector drops all Linux capabilities, enforces
    ``no-new-privileges:true``, binds the container port to the loopback
    host on an ephemeral host port, runs as the configured (or current)
    uid/gid, and mounts only the candidate worktree at ``/workspace:rw``.
    The canonical checkout and host credentials are never mounted.
    """
    if config.host_uid is not None:
        uid = config.host_uid
    else:
        uid = os.getuid() if hasattr(os, "getuid") else 0
    if config.host_gid is not None:
        gid = config.host_gid
    else:
        gid = os.getgid() if hasattr(os, "getgid") else 0

    argv: list[str] = [
        config.docker_binary,
        "run",
        "-d",
        "--rm",
        "--name",
        container_name,
        "-p",
        f"{config.loopback_host}:0:{config.container_port}",
        "-u",
        f"{uid}:{gid}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "-v",
        f"{candidate_root.resolve()}:/workspace:rw",
        "-w",
        "/workspace",
        "-e",
        f"OH_SESSION_API_KEYS_0={session_api_key}",
        "-e",
        "OH_ENABLE_VNC=false",
    ]
    if config.network_mode == NetworkMode.NONE:
        argv.extend(["--network", "none"])
    if extra_env:
        for key in sorted(extra_env):
            argv.extend(["-e", f"{key}={extra_env[key]}"])
    argv.append(config.image)
    return argv


def _snapshot_tree(root: Path) -> dict[str, str]:
    """Map relative POSIX paths to sha256 hashes for a directory tree."""
    if not root.exists():
        return {}
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            snapshot[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def _diff_trees(root: Path, before: dict[str, str]) -> tuple[str, ...]:
    """Return sorted relative POSIX paths added, changed, or deleted since ``before``."""
    after = _snapshot_tree(root)
    changed = {rel for rel, digest in after.items() if before.get(rel) != digest}
    changed.update(rel for rel in before if rel not in after)
    return tuple(sorted(changed))


def _parse_mapped_port(output: str) -> int | None:
    """Extract the host port from ``docker port`` output like ``127.0.0.1:32768``."""
    for line in output.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        candidate = line.rsplit(":", 1)[-1].strip()
        if candidate.isdigit():
            return int(candidate)
    return None


@runtime_checkable
class OpenHandsRuntimeDriver(Protocol):
    """Transport-level protocol driving one OpenHands runtime container."""

    async def check_runtime_health(
        self, config: OpenHandsAdapterConfig
    ) -> tuple[bool, str | None]:
        """Assess whether the container runtime and pinned image are usable."""
        ...

    async def start_container(
        self,
        config: OpenHandsAdapterConfig,
        argv: Sequence[str],
        container_name: str,
    ) -> tuple[str, int]:
        """Start the container and return ``(container_id, mapped_host_port)``."""
        ...

    async def wait_healthy(
        self,
        host: str,
        port: int,
        session_api_key: str,
        timeout_seconds: float,
    ) -> bool:
        """Poll the runtime health endpoint until it is ready or timed out."""
        ...

    async def drive_action(
        self,
        host: str,
        port: int,
        session_api_key: str,
        request: ExecutionRequest,
        on_checkpoint: Callable[[ExecutorCheckpoint], None] | None = None,
    ) -> dict[str, Any]:
        """Dispatch the governed action and return the raw runtime response."""
        ...

    async def teardown_container(
        self, config: OpenHandsAdapterConfig, container_name: str
    ) -> bool:
        """Force-remove the container; return True when it is verified gone."""
        ...


class DockerOpenHandsRuntimeDriver:
    """Docker-backed implementation of :class:`OpenHandsRuntimeDriver`.

    All docker invocations use ``asyncio.create_subprocess_exec`` with fixed
    argument vectors — shell execution is never used. HTTP calls against the
    container use ``urllib.request`` inside ``asyncio.to_thread``, always
    carrying the ephemeral ``X-Session-API-Key`` header.
    """

    def __init__(
        self,
        *,
        docker_command_timeout_seconds: float = 60.0,
        http_timeout_seconds: float = 5.0,
        health_poll_interval_seconds: float = 0.25,
    ) -> None:
        self.docker_command_timeout_seconds = docker_command_timeout_seconds
        self.http_timeout_seconds = http_timeout_seconds
        self.health_poll_interval_seconds = health_poll_interval_seconds

    async def _run_docker(
        self, argv: Sequence[str]
    ) -> tuple[int, str, str]:
        """Run one docker argv vector; return (returncode, stdout, stderr)."""
        if not argv:
            raise ValueError("docker argv must not be empty")
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.docker_command_timeout_seconds
            )
        except asyncio.TimeoutError:
            with suppress(Exception):
                proc.kill()
                await proc.communicate()
            raise RuntimeError(
                f"docker command timed out after {self.docker_command_timeout_seconds}s"
            ) from None
        return (
            proc.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    async def check_runtime_health(
        self, config: OpenHandsAdapterConfig
    ) -> tuple[bool, str | None]:
        try:
            returncode, _stdout, _stderr = await self._run_docker(
                [config.docker_binary, "info"]
            )
        except Exception as exc:
            return False, f"docker binary is not usable: {exc}"
        if returncode != 0:
            return False, "docker daemon is not reachable (docker info failed)"
        try:
            returncode, _stdout, _stderr = await self._run_docker(
                [config.docker_binary, "image", "inspect", config.image]
            )
        except Exception as exc:
            return False, f"docker image inspect failed: {exc}"
        if returncode != 0:
            return False, f"pinned OpenHands image is not present locally: {config.image}"
        return True, None

    async def start_container(
        self,
        config: OpenHandsAdapterConfig,
        argv: Sequence[str],
        container_name: str,
    ) -> tuple[str, int]:
        returncode, stdout, stderr = await self._run_docker(argv)
        if returncode != 0:
            raise RuntimeError(
                f"docker run failed for container {container_name}: {stderr.strip()}"
            )
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if not lines:
            raise RuntimeError(
                f"docker run produced no container id for container {container_name}"
            )
        container_id = lines[0]
        returncode, stdout, stderr = await self._run_docker(
            [config.docker_binary, "port", container_name, str(config.container_port)]
        )
        if returncode != 0:
            raise RuntimeError(
                f"unable to resolve mapped port for container {container_name}: {stderr.strip()}"
            )
        mapped_port = _parse_mapped_port(stdout)
        if mapped_port is None:
            raise RuntimeError(
                f"docker port returned no mapped host port for container {container_name}"
            )
        return container_id, mapped_port

    def _http_get_status(self, url: str, session_api_key: str) -> int:
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"X-Session-API-Key": session_api_key},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.http_timeout_seconds) as resp:
                return int(resp.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)

    async def wait_healthy(
        self,
        host: str,
        port: int,
        session_api_key: str,
        timeout_seconds: float,
    ) -> bool:
        url = f"http://{host}:{port}/health"
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        while True:
            try:
                status = await asyncio.to_thread(self._http_get_status, url, session_api_key)
                if status == 200:
                    return True
            except Exception:
                pass
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self.health_poll_interval_seconds)

    def _http_json(
        self,
        method: str,
        url: str,
        session_api_key: str,
        payload: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "X-Session-API-Key": session_api_key,
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(request, timeout=self.http_timeout_seconds) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        if not body:
            return {}
        parsed = json.loads(body)
        if not isinstance(parsed, dict):
            raise RuntimeError(f"non-object JSON response from {method} {url}")
        return parsed

    async def drive_action(
        self,
        host: str,
        port: int,
        session_api_key: str,
        request: ExecutionRequest,
        on_checkpoint: Callable[[ExecutorCheckpoint], None] | None = None,
    ) -> dict[str, Any]:
        base_url = f"http://{host}:{port}"
        payload = {
            "initial_user_msg": request.mandate,
            "allowed_tools": sorted(request.allowed_tools),
            "denied_tools": sorted(request.denied_tools),
        }
        created = await asyncio.to_thread(
            self._http_json,
            "POST",
            f"{base_url}/api/conversations",
            session_api_key,
            payload,
        )
        conversation_id = str(created.get("conversation_id") or created.get("id") or "")
        if not conversation_id:
            raise RuntimeError("OpenHands conversation creation returned no conversation id")
        if on_checkpoint is not None:
            on_checkpoint(
                ExecutorCheckpoint(
                    checkpoint_id=f"oh-drv-{secrets.token_hex(6)}",
                    execution_id=request.execution_id,
                    sequence=1,
                    summary="OpenHands conversation created and action dispatched",
                    created_at=_now_iso(),
                    diagnostics={"conversation_id": conversation_id},
                )
            )
        status = await asyncio.to_thread(
            self._http_json,
            "GET",
            f"{base_url}/api/conversations/{conversation_id}",
            session_api_key,
            None,
        )
        return {
            "conversation_id": conversation_id,
            "status": str(status.get("status", "unknown")),
            "response": status,
        }

    async def teardown_container(
        self, config: OpenHandsAdapterConfig, container_name: str
    ) -> bool:
        try:
            returncode, _stdout, stderr = await self._run_docker(
                [config.docker_binary, "rm", "-f", container_name]
            )
        except Exception:
            return False
        if returncode == 0:
            return True
        lowered = stderr.lower()
        if "no such container" in lowered or "no such object" in lowered:
            return True
        return False


class OpenHandsExecutor(ExecutorPort):
    """ExecutorPort adapter running governed actions in a pinned OpenHands container.

    Each execution launches a disposable, hardened container with only the
    candidate worktree mounted, drives one action, and guarantees teardown.
    Results never confer promotion authority.
    """

    def __init__(
        self,
        config: OpenHandsAdapterConfig | None = None,
        driver: OpenHandsRuntimeDriver | None = None,
    ) -> None:
        self.config = config if config is not None else OpenHandsAdapterConfig()
        self.driver = driver if driver is not None else DockerOpenHandsRuntimeDriver()
        self._active_tasks: dict[str, asyncio.Task[None]] = {}
        self._active_containers: dict[str, str] = {}

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            supports_resume=False,
            supports_streaming=False,
            supports_cancellation=True,
            supports_checkpoints=True,
            supported_network_modes=(NetworkMode.NONE, NetworkMode.LOOPBACK_ONLY),
            requires_container_runtime=True,
            supports_host_containment=True,
            declared_tools=("Read", "Edit", "Write", "Glob", "Grep"),
        )

    async def health(self) -> ExecutorHealthStatus:
        healthy, reason = await self.driver.check_runtime_health(self.config)
        return ExecutorHealthStatus(
            adapter_id=ADAPTER_ID,
            healthy=healthy,
            reason=reason,
            containment_available=healthy,
            details={
                "image": self.config.image,
                "network_mode": self.config.network_mode.value,
            },
        )

    async def start(
        self,
        request: ExecutionRequest,
        on_checkpoint: Callable[[ExecutorCheckpoint], None] | None = None,
    ) -> ExecutionResult:
        """Run one governed action in a disposable OpenHands container.

        The candidate tree is snapshotted before launch and re-scanned after
        teardown to compute ``modified_paths`` (relative POSIX paths only).
        A ``try ... finally`` block guarantees container teardown on every
        exit path, including crash, timeout, and cancellation.
        """
        caps = self.capabilities()
        started_at = time.monotonic()
        candidate_root = request.candidate_root
        before_snapshot = _snapshot_tree(candidate_root)
        session_api_key = secrets.token_urlsafe(24)
        container_name = f"mcp-oh-{request.execution_id[:16]}-{secrets.token_hex(4)}"

        emitted: list[ExecutorCheckpoint] = []

        def record(checkpoint: ExecutorCheckpoint) -> None:
            emitted.append(checkpoint)
            if on_checkpoint is not None:
                on_checkpoint(checkpoint)

        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_tasks[request.execution_id] = current_task
        self._active_containers[request.execution_id] = container_name

        argv = build_openhands_container_argv(
            self.config,
            container_name=container_name,
            candidate_root=candidate_root,
            session_api_key=session_api_key,
            extra_env={str(k): str(v) for k, v in request.env_projection.items()},
        )

        state = ExecutorLifecycleState.FAILED
        summary = "OpenHands execution failed"
        error_code: str | None = None
        error_message: str | None = None
        action_result: dict[str, Any] | None = None
        teardown_verified = False

        try:
            if caps.supports_checkpoints:
                record(
                    ExecutorCheckpoint(
                        checkpoint_id=f"oh-chk-{request.execution_id}-{secrets.token_hex(4)}",
                        execution_id=request.execution_id,
                        sequence=1,
                        summary="OpenHands execution request received; container launch initiated",
                        created_at=_now_iso(),
                        diagnostics={
                            "container_name": container_name,
                            "image": self.config.image,
                            "network_mode": self.config.network_mode.value,
                        },
                    )
                )

            if request.resource_limits.network_mode not in caps.supported_network_modes:
                state = ExecutorLifecycleState.FAILED
                summary = "Requested network mode is not supported by the OpenHands adapter"
                error_code = "OPENHANDS_CAPABILITY_MISMATCH"
                error_message = (
                    "network mode "
                    f"{request.resource_limits.network_mode.value} is not supported; "
                    f"allowed: {[m.value for m in caps.supported_network_modes]}"
                )
            else:
                container_started = False
                try:
                    _container_id, mapped_port = await self.driver.start_container(
                        self.config, argv, container_name
                    )
                    container_started = True
                except Exception as exc:
                    state = ExecutorLifecycleState.FAILED
                    summary = "OpenHands container failed to start"
                    error_code = "OPENHANDS_CONTAINER_START_FAILED"
                    error_message = _scrub_secrets(
                        f"{type(exc).__name__}: {exc}", (session_api_key,)
                    )

                if container_started:
                    try:
                        healthy = await self.driver.wait_healthy(
                            self.config.loopback_host,
                            mapped_port,
                            session_api_key,
                            self.config.startup_timeout_seconds,
                        )
                    except Exception as exc:
                        healthy = False
                        state = ExecutorLifecycleState.FAILED
                        summary = "OpenHands runtime health check failed"
                        error_code = "OPENHANDS_HEALTHCHECK_ERROR"
                        error_message = _scrub_secrets(
                            f"{type(exc).__name__}: {exc}", (session_api_key,)
                        )

                    if healthy:
                        execution_timeout = min(
                            self.config.execution_timeout_seconds,
                            request.resource_limits.timeout_seconds,
                        )
                        try:
                            action_result = await asyncio.wait_for(
                                self.driver.drive_action(
                                    self.config.loopback_host,
                                    mapped_port,
                                    session_api_key,
                                    request,
                                    record,
                                ),
                                timeout=execution_timeout,
                            )
                        except asyncio.TimeoutError:
                            state = ExecutorLifecycleState.TIMED_OUT
                            summary = "OpenHands action exceeded the execution timeout"
                            error_code = "OPENHANDS_EXECUTION_TIMEOUT"
                            error_message = (
                                f"action did not finish within {execution_timeout}s"
                            )
                        except Exception as exc:
                            state = ExecutorLifecycleState.FAILED
                            summary = "OpenHands runtime crashed while driving the action"
                            error_code = "OPENHANDS_RUNTIME_CRASH"
                            error_message = _scrub_secrets(
                                f"{type(exc).__name__}: {exc}", (session_api_key,)
                            )
                        else:
                            state = ExecutorLifecycleState.COMPLETED
                            summary = "OpenHands runtime completed the action"
                    elif error_code is None:
                        state = ExecutorLifecycleState.TIMED_OUT
                        summary = "OpenHands runtime did not become healthy in time"
                        error_code = "OPENHANDS_STARTUP_TIMEOUT"
                        error_message = (
                            "runtime health endpoint was not ready within "
                            f"{self.config.startup_timeout_seconds}s"
                        )
        except Exception as exc:
            state = ExecutorLifecycleState.FAILED
            summary = "OpenHands executor encountered an internal error"
            error_code = "OPENHANDS_INTERNAL_ERROR"
            error_message = _scrub_secrets(
                f"{type(exc).__name__}: {exc}", (session_api_key,)
            )
        finally:
            try:
                teardown_verified = await self.driver.teardown_container(
                    self.config, container_name
                )
            except Exception:
                teardown_verified = False
            self._active_containers.pop(request.execution_id, None)
            self._active_tasks.pop(request.execution_id, None)

        modified_paths = _diff_trees(candidate_root, before_snapshot)
        duration_ms = int((time.monotonic() - started_at) * 1000)
        exit_code: int | None = None
        if isinstance(action_result, Mapping):
            raw_exit = action_result.get("exit_code")
            if isinstance(raw_exit, bool):
                exit_code = None
            elif isinstance(raw_exit, int):
                exit_code = raw_exit
        vendor_diagnostics = {
            "openhands": redact_openhands_diagnostics(
                {
                    "image": self.config.image,
                    "container_name": container_name,
                    "teardown_verified": teardown_verified,
                },
                secrets=(session_api_key,),
            )
        }
        resource_usage = ExecutorResourceUsage(
            duration_ms=duration_ms,
            turns_used=1 if state == ExecutorLifecycleState.COMPLETED else 0,
            exit_code=exit_code,
        )

        if state == ExecutorLifecycleState.COMPLETED:
            return ExecutionResult(
                execution_id=request.execution_id,
                adapter_id=ADAPTER_ID,
                state=state,
                summary=summary,
                modified_paths=modified_paths,
                checkpoints=tuple(emitted),
                resource_usage=resource_usage,
                vendor_diagnostics=vendor_diagnostics,
                containment_profile="docker_hardened",
                grants_authority=False,
            )
        return ExecutionResult(
            execution_id=request.execution_id,
            adapter_id=ADAPTER_ID,
            state=state,
            summary=summary,
            modified_paths=modified_paths,
            checkpoints=tuple(emitted),
            resource_usage=resource_usage,
            error_code=error_code,
            error_message=error_message,
            vendor_diagnostics=vendor_diagnostics,
            containment_profile="docker_hardened",
            grants_authority=False,
        )

    async def resume(
        self,
        request: ExecutionRequest,
        checkpoint: ExecutorCheckpoint,
        on_checkpoint: Callable[[ExecutorCheckpoint], None] | None = None,
    ) -> ExecutionResult:
        """OpenHands executions are disposable; resume semantics are unsupported."""
        return ExecutionResult(
            execution_id=request.execution_id,
            adapter_id=ADAPTER_ID,
            state=ExecutorLifecycleState.FAILED,
            summary="OpenHands executor does not support resume semantics",
            error_code="OPENHANDS_RESUME_UNSUPPORTED",
            error_message="OpenHands executor does not support resume semantics",
            grants_authority=False,
        )

    async def cancel(
        self,
        execution_id: str,
        reason: str = "cancelled_by_caller",
    ) -> bool:
        """Cancel a running execution and guarantee container teardown.

        Returns True whether or not the execution was still running — a
        cancellation request for an unknown or already-finished execution is
        idempotently satisfied.
        """
        task = self._active_tasks.get(execution_id)
        container_name = self._active_containers.get(execution_id)

        if (
            task is not None
            and not task.done()
            and task is not asyncio.current_task()
        ):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        if container_name is not None:
            try:
                await self.driver.teardown_container(self.config, container_name)
            except Exception:
                pass
            self._active_containers.pop(execution_id, None)
        self._active_tasks.pop(execution_id, None)
        return True
