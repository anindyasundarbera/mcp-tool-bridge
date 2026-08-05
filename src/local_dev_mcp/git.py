from __future__ import annotations

import asyncio
from pathlib import Path


class GitCommandError(RuntimeError):
    pass


async def run_git(root: Path, *arguments: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        *arguments,
        cwd=str(root),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise GitCommandError(
            f"git {' '.join(arguments)} failed with exit code {process.returncode}: {detail}"
        )
    return stdout.decode("utf-8", errors="replace").strip()


async def project_status(root: Path) -> dict[str, object]:
    branch, head, status, diff_stat = await asyncio.gather(
        run_git(root, "branch", "--show-current"),
        run_git(root, "rev-parse", "HEAD"),
        run_git(root, "status", "--short"),
        run_git(root, "diff", "--stat", "HEAD"),
    )
    return {
        "branch": branch,
        "head": head,
        "clean": not bool(status),
        "working_tree": status,
        "diff_stat": diff_stat,
    }

