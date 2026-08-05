from __future__ import annotations

import asyncio
import os
import signal
import stat
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastmcp import Client

from .config import Settings

EXPECTED_CANDIDATE_TOOLS = frozenset(
    {
        "list_allowed_projects",
        "send_to_local_dev",
        "get_development_job",
        "resume_development_job",
        "cancel_development_job",
    }
)


async def _probe_fastmcp(url: str) -> list[str]:
    async with Client(url, timeout=2, init_timeout=2) as client:
        tools = await client.list_tools()
    return sorted(tool.name for tool in tools)


async def _stop_process(process: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        return await asyncio.wait_for(process.communicate(), timeout=5)
    except TimeoutError:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return await process.communicate()


class CandidateHealthChecker:
    def __init__(
        self,
        settings: Settings,
        probe: Callable[[str], Awaitable[list[str]]] | None = None,
    ) -> None:
        self.settings = settings
        self._probe = probe or _probe_fastmcp
        self._lock = asyncio.Lock()

    async def check(self, root: Path) -> dict[str, Any]:
        async with self._lock:
            return await self._check(root)

    async def _check(self, root: Path) -> dict[str, Any]:
        entrypoint = root / "server.py"
        try:
            metadata = os.lstat(entrypoint)
        except FileNotFoundError:
            return {
                "status": "unhealthy",
                "port": self.settings.candidate_mcp_port,
                "tools": [],
                "error": "candidate server.py is missing",
            }
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return {
                "status": "unhealthy",
                "port": self.settings.candidate_mcp_port,
                "tools": [],
                "error": "candidate server.py is not a trusted regular file",
            }

        environment = {
            name: os.environ[name]
            for name in ("PATH", "LANG", "LC_ALL", "TZ", "TMPDIR")
            if name in os.environ
        }
        environment.update(
            {
                "PROJECTS_CONFIG_PATH": str(self.settings.projects_config_path),
                "MCP_HOST": "127.0.0.1",
                "MCP_PORT": str(self.settings.candidate_mcp_port),
                "NO_COLOR": "1",
            }
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(entrypoint),
            cwd=str(root),
            env=environment,
            start_new_session=True,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        url = f"http://127.0.0.1:{self.settings.candidate_mcp_port}/mcp"
        deadline = time.monotonic() + self.settings.candidate_health_timeout_seconds
        tools: list[str] = []
        error = "candidate health probe timed out"
        healthy = False
        stdout = b""
        stderr = b""
        try:
            while time.monotonic() < deadline:
                if process.returncode is not None:
                    error = f"candidate exited with code {process.returncode}"
                    break
                remaining = deadline - time.monotonic()
                try:
                    tools = await asyncio.wait_for(
                        self._probe(url), timeout=max(0.1, min(2.0, remaining))
                    )
                    missing = sorted(EXPECTED_CANDIDATE_TOOLS.difference(tools))
                    if missing:
                        error = "candidate is missing expected tools: " + ", ".join(missing)
                    else:
                        healthy = True
                    break
                except Exception as exc:
                    error = str(exc) or type(exc).__name__
                    await asyncio.sleep(0.1)
        finally:
            stdout, stderr = await _stop_process(process)

        response: dict[str, Any] = {
            "status": "healthy" if healthy else "unhealthy",
            "port": self.settings.candidate_mcp_port,
            "tools": tools,
        }
        if not healthy:
            diagnostics = stderr.decode("utf-8", errors="replace").strip()
            if not diagnostics:
                diagnostics = stdout.decode("utf-8", errors="replace").strip()
            response["error"] = error[-2000:]
            if diagnostics:
                response["diagnostics"] = diagnostics[-4000:]
        return response
