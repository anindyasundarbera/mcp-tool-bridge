from __future__ import annotations

import asyncio
import json
import os
import signal
from typing import Any

from .config import Settings
from .git import project_status
from .models import Effort, ProjectPolicy


def validate_effort(project: ProjectPolicy, effort: Effort) -> None:
    if effort == "ultracode" and not project.allow_ultracode:
        raise ValueError(f"project {project.id!r} does not allow ultracode")


def build_claude_command(
    settings: Settings,
    project: ProjectPolicy,
    effort: Effort,
) -> list[str]:
    validate_effort(project, effort)
    model = project.claude_model or settings.claude_model
    max_turns = project.claude_max_turns or settings.claude_max_turns
    return [
        "ollama",
        "launch",
        "claude",
        "--model",
        model,
        "--yes",
        "--",
        "-p",
        "--permission-mode",
        "acceptEdits",
        "--tools",
        "Read,Glob,Grep,Edit,Write",
        "--disallowedTools",
        "Bash",
        "WebFetch",
        "WebSearch",
        "mcp__*",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--max-turns",
        str(max_turns),
        "--output-format",
        "json",
        "--effort",
        effort,
    ]


async def terminate_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


class ClaudeExecutor:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def execute(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: Effort,
    ) -> dict[str, Any]:
        text = specification.strip()
        if not text:
            raise ValueError("text must not be empty")
        if len(text) > self.settings.max_input_chars:
            raise ValueError(
                f"text exceeds the {self.settings.max_input_chars}-character limit"
            )

        before = await project_status(project.root)
        command = build_claude_command(self.settings, project, effort)
        environment = os.environ.copy()
        environment["NO_COLOR"] = "1"

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(project.root),
            env=environment,
            start_new_session=True,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(text.encode("utf-8")),
                timeout=self.settings.claude_timeout_seconds,
            )
        except asyncio.TimeoutError:
            await terminate_process_group(process)
            return {
                "status": "timed_out",
                "project_id": project.id,
                "model": project.claude_model or self.settings.claude_model,
                "effort": effort,
                "timeout_seconds": self.settings.claude_timeout_seconds,
                "repository": await project_status(project.root),
            }

        after = await project_status(project.root)
        if len(stdout_bytes) > self.settings.max_output_bytes:
            return {
                "status": "failed",
                "project_id": project.id,
                "model": project.claude_model or self.settings.claude_model,
                "effort": effort,
                "exit_code": process.returncode,
                "error": "Claude output exceeded the configured size limit",
                "repository_before": before,
                "repository_after": after,
            }

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        try:
            claude_result = json.loads(stdout)
        except json.JSONDecodeError:
            claude_result = {
                "is_error": process.returncode != 0,
                "result": stdout,
            }

        succeeded = process.returncode == 0 and not claude_result.get("is_error", False)
        response: dict[str, Any] = {
            "status": "completed" if succeeded else "failed",
            "project_id": project.id,
            "project_name": project.display_name,
            "model": project.claude_model or self.settings.claude_model,
            "effort": effort,
            "exit_code": process.returncode,
            "result": claude_result.get("result", ""),
            "session_id": claude_result.get("session_id"),
            "terminal_reason": claude_result.get("terminal_reason"),
            "subtype": claude_result.get("subtype"),
            "errors": claude_result.get("errors", []),
            "repository_before": before,
            "repository_after": after,
        }
        if stderr:
            response["stderr"] = stderr[-4000:]
        return response

