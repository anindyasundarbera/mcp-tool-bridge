"""Service lifecycle management for the local Conrad MCP bridge.

M3.1 (A3.1.2): Manages Conrad background and foreground service lifecycle,
instance PID files, atomic state tracking, liveness probing, port conflict
detection, and graceful shutdown without zero orphaned state or recycled PID signaling.

Deliberate constraints:
- Zero fastmcp imports at module top-level.
- Strict 0o600 permissions on PID files, 0o700 on state directory.
- Atomic file writes using temporary files and os.replace.
- Non-recycled PID verification on Linux via /proc/<pid>/cmdline.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Mapping
import uuid

from . import __version__
from .transports import TransportConfig, TransportMode

_SPAWNED_PROCS: dict[int, subprocess.Popen[Any]] = {}


class ServiceState(str, Enum):
    """Lifecycle state of a bridge service instance."""

    RUNNING = "running"
    STOPPED = "stopped"
    STALE = "stale"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class ServiceInstanceRecord:
    """Persistent metadata for an active or recorded bridge service instance."""

    instance_id: str
    pid: int
    started_at: str
    transport_mode: str
    host: str
    port: int | None
    config_path: str
    state_dir: str
    command_fingerprint: str
    version: str

    def to_dict(self) -> dict[str, Any]:
        """Convert record to JSON-serializable dictionary."""
        return {
            "instance_id": self.instance_id,
            "pid": self.pid,
            "started_at": self.started_at,
            "transport_mode": self.transport_mode,
            "host": self.host,
            "port": self.port,
            "config_path": self.config_path,
            "state_dir": self.state_dir,
            "command_fingerprint": self.command_fingerprint,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ServiceInstanceRecord:
        """Hydrate record from serialized dictionary."""
        port_raw = data.get("port")
        return cls(
            instance_id=str(data["instance_id"]),
            pid=int(data["pid"]),
            started_at=str(data["started_at"]),
            transport_mode=str(data["transport_mode"]),
            host=str(data["host"]),
            port=int(port_raw) if port_raw is not None else None,
            config_path=str(data["config_path"]),
            state_dir=str(data["state_dir"]),
            command_fingerprint=str(data["command_fingerprint"]),
            version=str(data["version"]),
        )

    def save(self, path: Path) -> None:
        """Atomically persist record to path with 0o600 permissions."""
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(f".tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True)
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(payload)
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, path)
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    @classmethod
    def load(cls, path: Path) -> ServiceInstanceRecord | None:
        """Safely load record from path, returning None on missing or corrupted file."""
        if not path.is_file():
            return None
        try:
            content = path.read_text(encoding="utf-8")
            data = json.loads(content)
            if not isinstance(data, dict):
                return None
            return cls.from_dict(data)
        except Exception:
            return None


@dataclass(frozen=True)
class ServiceStatusReport:
    """Report describing the current status of the service."""

    state: ServiceState
    pid: int | None
    instance_id: str | None
    transport_mode: str | None
    host: str | None
    port: int | None
    config_path: str
    state_dir: str
    stale_pid_recovered: bool = False
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert report to JSON-serializable dictionary."""
        return {
            "state": self.state.value if isinstance(self.state, ServiceState) else str(self.state),
            "pid": self.pid,
            "instance_id": self.instance_id,
            "transport_mode": self.transport_mode,
            "host": self.host,
            "port": self.port,
            "config_path": self.config_path,
            "state_dir": self.state_dir,
            "stale_pid_recovered": self.stale_pid_recovered,
            "reason": self.reason,
        }


class ServiceLifecycleManager:
    """Manages start, status, stop, and cleanup of Conrad bridge service instances."""

    def __init__(
        self,
        config_path: Path,
        state_dir: Path,
        pid_file: Path | None = None,
        process_spawner: Callable[..., int] | None = None,
        pid_checker: Callable[[int, str], bool] | None = None,
        signal_sender: Callable[[int, int], None] | None = None,
        port_probe: Callable[[str, int], None] | None = None,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self.state_dir = Path(state_dir).resolve()
        self.pid_file = (
            Path(pid_file).resolve()
            if pid_file is not None
            else (self.state_dir / "conrad-service.json")
        )
        self._process_spawner = process_spawner
        self._pid_checker = pid_checker or self._default_pid_checker
        self._signal_sender = signal_sender or self._default_signal_sender
        self._port_probe = port_probe or self._default_port_probe

    @staticmethod
    def _default_pid_checker(pid: int, expected_fingerprint: str) -> bool:
        """Verify process exists and is a Conrad/Python service instance rather than recycled PID."""
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OSError):
            return False

        proc_cmdline = Path(f"/proc/{pid}/cmdline")
        if proc_cmdline.exists():
            try:
                cmdline_bytes = proc_cmdline.read_bytes()
                if not cmdline_bytes:
                    return False
                cmdline = cmdline_bytes.decode("utf-8", errors="replace").replace("\x00", " ")
                tokens = ["local_dev_mcp", "conrad", "python"]
                if expected_fingerprint:
                    tokens.append(expected_fingerprint)
                return any(token in cmdline for token in tokens)
            except (PermissionError, FileNotFoundError, OSError):
                return False

        return True

    @staticmethod
    def _default_signal_sender(pid: int, sig: int) -> None:
        """Send signal to target PID."""
        os.kill(pid, sig)

    @staticmethod
    def _default_port_probe(host: str, port: int) -> None:
        """Verify loopback host:port availability, raising OSError on collision."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
        except OSError as exc:
            raise OSError(f"Port {port} on {host} is in use: {exc}") from exc
        finally:
            sock.close()

    def status(self) -> ServiceStatusReport:
        """Inspect service status, automatically recovering stale or corrupt PID records."""
        if not self.pid_file.is_file():
            return ServiceStatusReport(
                state=ServiceState.STOPPED,
                pid=None,
                instance_id=None,
                transport_mode=None,
                host=None,
                port=None,
                config_path=str(self.config_path),
                state_dir=str(self.state_dir),
                stale_pid_recovered=False,
                reason=None,
            )

        record = ServiceInstanceRecord.load(self.pid_file)
        if record is None:
            self.pid_file.unlink(missing_ok=True)
            return ServiceStatusReport(
                state=ServiceState.STOPPED,
                pid=None,
                instance_id=None,
                transport_mode=None,
                host=None,
                port=None,
                config_path=str(self.config_path),
                state_dir=str(self.state_dir),
                stale_pid_recovered=True,
                reason="Removed corrupt PID record",
            )

        is_alive = self._pid_checker(record.pid, record.command_fingerprint)
        if not is_alive:
            self.pid_file.unlink(missing_ok=True)
            return ServiceStatusReport(
                state=ServiceState.STOPPED,
                pid=None,
                instance_id=None,
                transport_mode=None,
                host=None,
                port=None,
                config_path=str(self.config_path),
                state_dir=str(self.state_dir),
                stale_pid_recovered=True,
                reason="Removed stale PID record",
            )

        return ServiceStatusReport(
            state=ServiceState.RUNNING,
            pid=record.pid,
            instance_id=record.instance_id,
            transport_mode=record.transport_mode,
            host=record.host,
            port=record.port,
            config_path=record.config_path,
            state_dir=record.state_dir,
            stale_pid_recovered=False,
            reason=None,
        )

    def start(
        self,
        *,
        foreground: bool = False,
        transport_mode: TransportMode = TransportMode.STDIO,
        host: str = "127.0.0.1",
        port: int = 8000,
        foreground_runner: Callable[[], int] | None = None,
    ) -> ServiceStatusReport:
        """Start the bridge service in foreground or managed background mode."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:
            pass

        current_status = self.status()
        if current_status.state == ServiceState.RUNNING:
            raise RuntimeError("Service is already running for this state directory")

        # Validate transport configuration
        _ = TransportConfig(
            mode=transport_mode,
            host=host,
            port=port,
        )

        if transport_mode == TransportMode.LOCAL_HTTP:
            self._port_probe(host, port)

        instance_id = f"inst_{uuid.uuid4().hex[:12]}"
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        fingerprint = f"local_dev_mcp:{transport_mode.value}:{host}:{port}"

        if foreground:
            pid = os.getpid()
            record = ServiceInstanceRecord(
                instance_id=instance_id,
                pid=pid,
                started_at=started_at,
                transport_mode=transport_mode.value,
                host=host,
                port=port if transport_mode != TransportMode.STDIO else None,
                config_path=str(self.config_path),
                state_dir=str(self.state_dir),
                command_fingerprint=fingerprint,
                version=__version__,
            )
            record.save(self.pid_file)

            prev_sigterm = None
            prev_sigint = None
            if threading.current_thread() is threading.main_thread():
                def _sig_handler(signum: int, frame: Any) -> None:
                    self.pid_file.unlink(missing_ok=True)
                    sys.exit(0)

                try:
                    prev_sigterm = signal.signal(signal.SIGTERM, _sig_handler)
                    prev_sigint = signal.signal(signal.SIGINT, _sig_handler)
                except (ValueError, OSError):
                    pass

            try:
                if foreground_runner is not None:
                    foreground_runner()
            finally:
                self.pid_file.unlink(missing_ok=True)
                if prev_sigterm is not None:
                    try:
                        signal.signal(signal.SIGTERM, prev_sigterm)
                    except (ValueError, OSError):
                        pass
                if prev_sigint is not None:
                    try:
                        signal.signal(signal.SIGINT, prev_sigint)
                    except (ValueError, OSError):
                        pass

            return ServiceStatusReport(
                state=ServiceState.STOPPED,
                pid=None,
                instance_id=instance_id,
                transport_mode=transport_mode.value,
                host=host,
                port=port,
                config_path=str(self.config_path),
                state_dir=str(self.state_dir),
                stale_pid_recovered=False,
                reason="Foreground runner completed",
            )

        # Managed background mode
        if self._process_spawner is not None:
            pid = self._process_spawner(
                config_path=self.config_path,
                state_dir=self.state_dir,
                pid_file=self.pid_file,
                transport_mode=transport_mode,
                host=host,
                port=port,
            )
        else:
            log_path = self.state_dir / "conrad-service.log"
            with open(log_path, "a", encoding="utf-8") as log_file:
                cmd = [
                    sys.executable,
                    "-m",
                    "local_dev_mcp.cli",
                    "--config",
                    str(self.config_path),
                    "--state-dir",
                    str(self.state_dir),
                    "start",
                    "--foreground",
                    "--transport",
                    transport_mode.value,
                    "--host",
                    host,
                    "--port",
                    str(port),
                    "--pid-file",
                    str(self.pid_file),
                ]
                safe_env = {
                    k: v
                    for k, v in os.environ.items()
                    if not (
                        "TOKEN" in k.upper()
                        or "SECRET" in k.upper()
                        or "KEY" in k.upper()
                        or "PASS" in k.upper()
                    )
                }
                safe_env["PYTHONPATH"] = os.environ.get("PYTHONPATH", "")
                safe_env["PATH"] = os.environ.get("PATH", "")
                proc = subprocess.Popen(
                    cmd,
                    start_new_session=True,
                    env=safe_env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )
                pid = proc.pid
                _SPAWNED_PROCS[pid] = proc

        record = ServiceInstanceRecord(
            instance_id=instance_id,
            pid=pid,
            started_at=started_at,
            transport_mode=transport_mode.value,
            host=host,
            port=port if transport_mode != TransportMode.STDIO else None,
            config_path=str(self.config_path),
            state_dir=str(self.state_dir),
            command_fingerprint=fingerprint,
            version=__version__,
        )
        record.save(self.pid_file)

        # Verify liveness via pid_checker
        deadline = time.time() + 2.5
        alive = False
        while time.time() < deadline:
            if self._pid_checker(pid, fingerprint):
                alive = True
                break
            time.sleep(0.05)

        if not alive:
            self.pid_file.unlink(missing_ok=True)
            raise RuntimeError("Background service failed to start or terminated unexpectedly")

        return ServiceStatusReport(
            state=ServiceState.RUNNING,
            pid=pid,
            instance_id=instance_id,
            transport_mode=transport_mode.value,
            host=host,
            port=port,
            config_path=str(self.config_path),
            state_dir=str(self.state_dir),
            stale_pid_recovered=False,
            reason=None,
        )

    def stop(self, *, timeout_seconds: float = 5.0) -> ServiceStatusReport:
        """Stop the running service instance and clean up PID file without residue."""
        report = self.status()
        if report.state == ServiceState.STOPPED:
            return ServiceStatusReport(
                state=ServiceState.STOPPED,
                pid=None,
                instance_id=None,
                transport_mode=None,
                host=None,
                port=None,
                config_path=str(self.config_path),
                state_dir=str(self.state_dir),
                stale_pid_recovered=report.stale_pid_recovered,
                reason="Service is already stopped",
            )

        pid = report.pid
        if pid is None:
            self.pid_file.unlink(missing_ok=True)
            return ServiceStatusReport(
                state=ServiceState.STOPPED,
                pid=None,
                instance_id=None,
                transport_mode=None,
                host=None,
                port=None,
                config_path=str(self.config_path),
                state_dir=str(self.state_dir),
                stale_pid_recovered=report.stale_pid_recovered,
                reason="No PID recorded",
            )

        record = ServiceInstanceRecord.load(self.pid_file)
        fingerprint = record.command_fingerprint if record else ""

        if not self._pid_checker(pid, fingerprint):
            self.pid_file.unlink(missing_ok=True)
            return ServiceStatusReport(
                state=ServiceState.STOPPED,
                pid=None,
                instance_id=None,
                transport_mode=None,
                host=None,
                port=None,
                config_path=str(self.config_path),
                state_dir=str(self.state_dir),
                stale_pid_recovered=True,
                reason="PID ownership verification failed; removed stale record without signaling",
            )

        try:
            self._signal_sender(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if not self._pid_checker(pid, fingerprint):
                break
            time.sleep(0.05)

        if self._pid_checker(pid, fingerprint):
            try:
                self._signal_sender(pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            kill_deadline = time.time() + 1.0
            while time.time() < kill_deadline:
                if not self._pid_checker(pid, fingerprint):
                    break
                time.sleep(0.05)

        # Reap child process if spawned by current process to avoid zombie/ResourceWarning
        proc = _SPAWNED_PROCS.pop(pid, None)
        if proc is not None:
            try:
                proc.wait(timeout=0.5)
            except Exception:
                pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass

        self.pid_file.unlink(missing_ok=True)
        return ServiceStatusReport(
            state=ServiceState.STOPPED,
            pid=None,
            instance_id=None,
            transport_mode=None,
            host=None,
            port=None,
            config_path=str(self.config_path),
            state_dir=str(self.state_dir),
            stale_pid_recovered=False,
            reason="Service stopped successfully",
        )


__all__ = [
    "ServiceState",
    "ServiceInstanceRecord",
    "ServiceStatusReport",
    "ServiceLifecycleManager",
]
