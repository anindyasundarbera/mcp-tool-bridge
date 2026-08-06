from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path


class GitCommandError(RuntimeError):
    pass


async def run_git(
    root: Path,
    *arguments: str,
    timeout_seconds: int = 30,
) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        *arguments,
        cwd=str(root),
        start_new_session=True,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds
        )
    except asyncio.TimeoutError as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        raise GitCommandError(
            f"git {' '.join(arguments)} timed out after {timeout_seconds} seconds"
        ) from exc
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise GitCommandError(
            f"git {' '.join(arguments)} failed with exit code {process.returncode}: {detail}"
        )
    return stdout.decode("utf-8", errors="replace").strip()


async def project_status(
    root: Path,
    timeout_seconds: int = 30,
) -> dict[str, object]:
    branch, head, status, diff_stat = await asyncio.gather(
        run_git(root, "branch", "--show-current", timeout_seconds=timeout_seconds),
        run_git(root, "rev-parse", "HEAD", timeout_seconds=timeout_seconds),
        run_git(root, "status", "--short", timeout_seconds=timeout_seconds),
        run_git(root, "diff", "--stat", "HEAD", timeout_seconds=timeout_seconds),
    )
    return {
        "branch": branch,
        "head": head,
        "clean": not bool(status),
        "working_tree": status,
        "diff_stat": diff_stat,
    }
