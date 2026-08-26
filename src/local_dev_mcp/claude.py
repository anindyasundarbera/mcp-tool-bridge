from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path
from typing import Any

from .config import Settings
from .git import project_status
from .models import Effort, ProjectPolicy


def validate_effort(project: ProjectPolicy, effort: Effort) -> None:
    if effort == "ultracode" and not project.allow_ultracode:
        raise ValueError(f"project {project.id!r} does not allow ultracode")


def _substitute_model(
    template: tuple[str, ...], model: str
) -> list[str]:
    """Expand the launch template, replacing the standalone ``{model}`` token.

    Only an exact ``"{model}"`` element is replaced (token-equality), never a
    substring — so ``prefix{model}`` stays literal. The template (everything up
    to and including the operator-owned ``--`` passthrough, e.g.
    ``ollama launch claude --model {model} --yes -- -p``) is operator-owned via
    ``CLAUDE_LAUNCH_COMMAND``; the bridge appends the security tail afterwards.
    """
    return [model if token == "{model}" else token for token in template]


def build_claude_command(
    settings: Settings,
    project: ProjectPolicy,
    effort: Effort,
    resume_session_id: str | None = None,
    *,
    max_turns_override: int | None = None,
) -> list[str]:
    validate_effort(project, effort)
    model = project.claude_model or settings.claude_model
    if max_turns_override is not None:
        if max_turns_override <= 0:
            raise ValueError("max_turns_override must be positive")
        max_turns = max_turns_override
    else:
        max_turns = project.claude_max_turns or settings.claude_max_turns
    command = _substitute_model(settings.claude_launch_command, model)
    if resume_session_id is not None:
        if (
            not resume_session_id
            or len(resume_session_id) > 512
            or any(ord(character) < 32 for character in resume_session_id)
        ):
            raise ValueError("stored Claude session ID is invalid")
        command.extend(["--resume", resume_session_id])
    command.extend(
        [
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
        "stream-json" if settings.claude_stream_json else "json",
        "--effort",
        effort,
        ]
    )
    if settings.claude_stream_json:
        # ``--verbose`` makes stream-json emit the full event sequence (system/
        # user/assistant/result) so the bridge can count turns and tool errors as
        # they happen. Omitted in plain ``json`` mode, where only the final
        # envelope is produced and the bridge relies on the progress sampler.
        command.append("--verbose")
    return command


def build_reviewer_command(
    settings: Settings,
    project: ProjectPolicy,
    effort: Effort,
    *,
    resume_session_id: str | None = None,
) -> list[str]:
    assert resume_session_id is None
    validate_effort(project, effort)
    model = project.claude_model or settings.claude_model
    max_turns = project.claude_max_turns or settings.claude_max_turns
    command = _substitute_model(settings.claude_review_launch_command, model)
    command.extend(
        [
            "--permission-mode",
            "default",
            "--tools",
            "Read,Glob,Grep",
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
    )
    return command


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


def _parse_output(stdout: str, *, stream: bool) -> dict[str, Any]:
    """Parse Claude's stdout into a result envelope.

    In plain ``json`` mode ``stdout`` is one JSON object. In ``stream-json``
    mode it is newline-delimited JSON events whose terminal ``type == "result"``
    event carries the same envelope fields (``result``, ``session_id``,
    ``num_turns``, ``usage``, ``subtype``, ``is_error``, ``terminal_reason``);
    ``type == "assistant"`` events are counted as turns and ``type == "user"``
    tool-result items with ``is_error`` as tool failures. Malformed lines are
    skipped so one bad event never loses the whole run. Falls back to a
    best-effort envelope (mirroring the json path) if no result event is found.
    """
    if not stream:
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            return {"is_error": True, "result": stdout}

    envelope: dict[str, Any] = {}
    assistant_turns = 0
    tool_failures = 0
    saw_result = False
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "assistant":
            assistant_turns += 1
        elif event_type == "user":
            for item in event.get("content") or []:
                if isinstance(item, dict) and item.get("is_error"):
                    tool_failures += 1
        elif event_type == "result":
            saw_result = True
            envelope = event
    if not saw_result:
        # No terminal result event: treat as an error with whatever was emitted.
        return {"is_error": True, "result": stdout, "num_turns": assistant_turns}
    # Surface turn/tool counts derived from the event stream alongside the
    # envelope so callers can use them even when the result event omits them.
    if "num_turns" not in envelope:
        envelope["num_turns"] = assistant_turns
    envelope.setdefault("tool_failures", tool_failures)
    return envelope


class ClaudeExecutor:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def _run_claude(
        self,
        command: list[str],
        project: ProjectPolicy,
        effort: Effort,
        execution_root: Path,
        text: str,
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        """Shared bounded-subprocess core for the drafter and the reviewer.

        Spawns the given Claude command, pipes ``text`` to stdin, bounds wall
        clock and output size, and parses the result envelope (one JSON object
        in plain ``json`` mode, or a newline-delimited event stream in
        ``stream-json`` mode). The caller shapes the public response; this
        method only reports what happened. On timeout the process group is
        killed and ``timed_out`` is reported.
        """
        before = await project_status(
            execution_root, self.settings.git_command_timeout_seconds
        )
        environment = os.environ.copy()
        environment["NO_COLOR"] = "1"

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(execution_root),
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
                "timed_out": True,
                "exit_code": process.returncode,
                "stderr": "",
                "claude_result": {},
                "before": before,
                "after": await project_status(
                    execution_root, self.settings.git_command_timeout_seconds
                ),
                "oversized": False,
            }
        except asyncio.CancelledError:
            await terminate_process_group(process)
            raise

        after = await project_status(
            execution_root, self.settings.git_command_timeout_seconds
        )
        oversized = len(stdout_bytes) > self.settings.max_output_bytes
        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        claude_result = _parse_output(stdout, stream=stream)
        if not stream and not claude_result:
            claude_result = {
                "is_error": process.returncode != 0,
                "result": stdout,
            }
        return {
            "timed_out": False,
            "exit_code": process.returncode,
            "stderr": stderr,
            "claude_result": claude_result,
            "before": before,
            "after": after,
            "oversized": oversized,
        }

    async def execute(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: Effort,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
        max_turns: int | None = None,
    ) -> dict[str, Any]:
        text = specification.strip()
        if not text:
            raise ValueError("text must not be empty")
        if len(text) > self.settings.max_input_chars:
            raise ValueError(
                f"text exceeds the {self.settings.max_input_chars}-character limit"
            )

        execution_root = project.root if root is None else root
        command = build_claude_command(
            self.settings,
            project,
            effort,
            resume_session_id,
            max_turns_override=max_turns,
        )
        outcome = await self._run_claude(
            command,
            project,
            effort,
            execution_root,
            text,
            stream=self.settings.claude_stream_json,
        )
        model = project.claude_model or self.settings.claude_model

        if outcome["timed_out"]:
            return {
                "status": "timed_out",
                "project_id": project.id,
                "model": model,
                "effort": effort,
                "timeout_seconds": self.settings.claude_timeout_seconds,
                "repository": outcome["after"],
            }
        if outcome["oversized"]:
            return {
                "status": "failed",
                "project_id": project.id,
                "model": model,
                "effort": effort,
                "exit_code": outcome["exit_code"],
                "error": "Claude output exceeded the configured size limit",
                "repository_before": outcome["before"],
                "repository_after": outcome["after"],
            }

        claude_result = outcome["claude_result"]
        succeeded = outcome["exit_code"] == 0 and not claude_result.get("is_error", False)
        response: dict[str, Any] = {
            "status": "completed" if succeeded else "failed",
            "project_id": project.id,
            "project_name": project.display_name,
            "model": model,
            "effort": effort,
            "exit_code": outcome["exit_code"],
            "result": claude_result.get("result", ""),
            "session_id": claude_result.get("session_id"),
            "terminal_reason": claude_result.get("terminal_reason"),
            "subtype": claude_result.get("subtype"),
            "errors": claude_result.get("errors", []),
            "usage": claude_result.get("usage"),
            "num_turns": claude_result.get("num_turns"),
            "tool_failures": claude_result.get("tool_failures", 0),
            "repository_before": outcome["before"],
            "repository_after": outcome["after"],
        }
        if outcome["stderr"]:
            response["stderr"] = outcome["stderr"][-4000:]
        return response

    async def execute_review(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: Effort,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Run a READ-ONLY, NON-RESUMING Claude reviewer over a candidate.

        Deliberately never resumes (``build_reviewer_command`` asserts
        ``resume_session_id is None``) so the reviewer is provably a fresh
        session distinct from the drafting agent, and never exposes a
        ``session_id`` in its result — a reviewer is one-shot and must not be
        resumable. The reviewer is launched with only ``Read,Glob,Grep`` and
        no shell/web/secondary-MCP tools, so it can inspect the candidate but
        cannot mutate it or exfiltrate.
        """
        assert resume_session_id is None
        text = specification.strip()
        if not text:
            raise ValueError("text must not be empty")
        if len(text) > self.settings.max_input_chars:
            raise ValueError(
                f"text exceeds the {self.settings.max_input_chars}-character limit"
            )

        execution_root = project.root if root is None else root
        command = build_reviewer_command(
            self.settings, project, effort, resume_session_id=None
        )
        outcome = await self._run_claude(
            command,
            project,
            effort,
            execution_root,
            text,
            stream=False,
        )
        model = project.claude_model or self.settings.claude_model

        if outcome["timed_out"]:
            return {
                "status": "timed_out",
                "project_id": project.id,
                "model": model,
                "effort": effort,
                "timeout_seconds": self.settings.claude_timeout_seconds,
                "repository": outcome["after"],
            }
        if outcome["oversized"]:
            return {
                "status": "failed",
                "project_id": project.id,
                "model": model,
                "effort": effort,
                "exit_code": outcome["exit_code"],
                "error": "reviewer output exceeded the configured size limit",
                "repository_before": outcome["before"],
                "repository_after": outcome["after"],
            }

        claude_result = outcome["claude_result"]
        succeeded = outcome["exit_code"] == 0 and not claude_result.get("is_error", False)
        response: dict[str, Any] = {
            "status": "completed" if succeeded else "failed",
            "project_id": project.id,
            "project_name": project.display_name,
            "model": model,
            "effort": effort,
            "exit_code": outcome["exit_code"],
            "result": claude_result.get("result", ""),
            "is_error": claude_result.get("is_error", False),
            "terminal_reason": claude_result.get("terminal_reason"),
            "subtype": claude_result.get("subtype"),
            "errors": claude_result.get("errors", []),
            "repository_before": outcome["before"],
            "repository_after": outcome["after"],
        }
        if outcome["stderr"]:
            response["stderr"] = outcome["stderr"][-4000:]
        return response
