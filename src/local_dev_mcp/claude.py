from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .config import Settings
from .containment import build_minimal_executor_env
from .executor_contract import (
    ExecutionRequest,
    ExecutionResult,
    ExecutorCapabilities,
    ExecutorCheckpoint,
    ExecutorHealthStatus,
    ExecutorLifecycleState,
    ExecutorResourceUsage,
    NetworkMode,
)
from .git import project_status
from .models import Effort, ProjectCapability, ProjectPolicy
from .security import projected_credential_env


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


# ---------------------------------------------------------------------------
# M-GV-02 named read-only roles: Planner, Evidence-officer, Supervisor
# ---------------------------------------------------------------------------
# Each role is a capability-gated *invocation* on the existing job/reader
# infrastructure — a one-shot, read-only Claude session launched with only
# ``Read,Glob,Grep`` (no Edit/Write, no shell/web/secondary-MCP tools, default
# permission mode, never resumed), mirroring ``build_reviewer_command``. The
# role's system prompt is passed as a standalone ``--append-system-prompt``
# argument so the launched session is self-describing; an optional ``context``
# (job/candidate evidence) is appended to that prompt so the role sees exactly
# the state it must act on. These are internal-first: no MCP tool, no new
# ``ProjectCapability`` in this milestone.

#: Bound on the combined role prompt + context so a role command stays a
#: bounded argument (AGENTS.md §7 bounds inputs/outputs).
_MAX_ROLE_PROMPT_CHARS = 20_000

PLANNER_ROLE_PROMPT = (
    "You are the Planner. Convert the ratified requirement set into a bounded "
    "milestone plan. You have read-only tools (Read, Glob, Grep) and must never "
    "edit files. Emit a single JSON object with keys \"milestones\" (a list of "
    "objects, each with \"id\", \"title\", \"scope\", and \"exit_criteria\") and "
    "\"assumptions\" (a list of strings)."
)

EVIDENCE_OFFICER_ROLE_PROMPT = (
    "You are the Evidence-officer. Verify the candidate evidence: check the diff "
    "fingerprint, the bindings, and requirement coverage. You have read-only "
    "tools (Read, Glob, Grep) and must never edit files. Emit a single JSON "
    "object with keys \"pass\" (bool), \"fingerprint_mismatch\" (bool), "
    "\"observed_fingerprint\" (string or null), \"coverage_ok\" (bool), and "
    "\"issues\" (list of strings). A fingerprint mismatch is a hard fail."
)

SUPERVISOR_ROLE_PROMPT = (
    "You are the Supervisor. Scan the checkpoints for loops, drift, and budget "
    "pressure. You have read-only tools (Read, Glob, Grep) and must never edit "
    "files. Emit a single JSON object with keys \"no_progress\" (bool), "
    "\"status\" (one of \"continue\", \"escalate\", \"narrow\", \"replace\"), "
    "and \"recommendations\" (list of strings). Flag stalled runs with no "
    "progress."
)


def _role_launch_command(
    settings: Settings,
    project: ProjectPolicy,
    effort: Effort,
    role_prompt: str,
    *,
    context: str | None = None,
) -> list[str]:
    """Build a one-shot, read-only role launch command (M-GV-02).

    Mirrors ``build_reviewer_command``: a fresh, non-resuming session with only
    ``Read,Glob,Grep``, default permission mode (never ``acceptEdits``), no
    shell, no web, and no secondary MCP tools. Reuses the operator-owned review
    launch template (``claude_review_launch_command``) — the role sessions are
    the same class of read-only invocation as the reviewer, so they share its
    launch prefix. The role prompt plus the optional ``context`` is passed as a
    standalone ``--append-system-prompt`` argument; the whole appended prompt is
    bounded by ``_MAX_ROLE_PROMPT_CHARS``.
    """
    validate_effort(project, effort)
    model = project.claude_model or settings.claude_model
    max_turns = project.claude_max_turns or settings.claude_max_turns
    prompt = role_prompt
    if context:
        prompt = f"{prompt}\n\nContext:\n{context}"
    if len(prompt) > _MAX_ROLE_PROMPT_CHARS:
        raise ValueError(
            f"role prompt exceeds the {_MAX_ROLE_PROMPT_CHARS}-character limit"
        )
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
            "--append-system-prompt",
            prompt,
        ]
    )
    return command


def build_planner_command(
    settings: Settings,
    project: ProjectPolicy,
    effort: Effort,
    *,
    context: str | None = None,
    resume_session_id: str | None = None,
) -> list[str]:
    """Build a read-only Planner command (M-GV-02).

    The Planner turns a ratified requirement set into a bounded milestone plan.
    Read-only (``Read,Glob,Grep``) and deliberately non-resuming; it emits a
    plan object (see ``parse_planner_output``) and never edits the repository.
    The plan feeds the milestone list (M-CT-04).
    """
    assert resume_session_id is None
    return _role_launch_command(
        settings, project, effort, PLANNER_ROLE_PROMPT, context=context
    )


def build_evidence_officer_command(
    settings: Settings,
    project: ProjectPolicy,
    effort: Effort,
    *,
    context: str | None = None,
    resume_session_id: str | None = None,
) -> list[str]:
    """Build a read-only Evidence-officer command (M-GV-02).

    The Evidence-officer checks fingerprints/bindings/coverage and emits a
    verdict (see ``parse_evidence_verdict``). Read-only and non-resuming. The
    verdict feeds the commit gate (M-GV-05).
    """
    assert resume_session_id is None
    return _role_launch_command(
        settings, project, effort, EVIDENCE_OFFICER_ROLE_PROMPT, context=context
    )


def build_supervisor_command(
    settings: Settings,
    project: ProjectPolicy,
    effort: Effort,
    *,
    context: str | None = None,
    resume_session_id: str | None = None,
) -> list[str]:
    """Build a read-only Supervisor command (M-GV-02).

    The Supervisor scans checkpoints for loops/drift/budget pressure and emits
    escalate/narrow/replace recommendations (see
    ``parse_supervisor_recommendations``). Read-only and non-resuming. The
    recommendations feed the control surface (M-CC-08).
    """
    assert resume_session_id is None
    return _role_launch_command(
        settings, project, effort, SUPERVISOR_ROLE_PROMPT, context=context
    )


def _result_text(result: Any) -> str:
    """Extract the Claude result text from an envelope dict or a raw string."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return str(result.get("result", ""))
    return str(result)


def _role_output_dict(result: Any, *, role: str) -> dict[str, Any]:
    """Parse a role's Claude result into a JSON object, or raise.

    Accepts the envelope ``_parse_output`` returns (or a raw JSON string). A
    role must emit a single JSON object; anything else (empty, malformed, or a
    non-object) fails closed with ``ValueError``.
    """
    text = _result_text(result).strip()
    if not text:
        raise ValueError(f"{role} produced no output")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{role} output is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{role} output must be a JSON object")
    return parsed


#: Upper bound on a Planner milestone list so a plan stays bounded.
_MAX_PLAN_MILESTONES = 128


def parse_planner_output(result: Any) -> list[dict[str, Any]]:
    """Parse Planner output into a bounded milestone list (M-GV-02).

    Accepts a Claude result envelope or a raw string. Returns the milestone
    objects (``{"id", "title", "scope", "exit_criteria"}``) — the input to the
    milestone list (M-CT-04). Fails closed on malformed/empty output, a missing
    ``id``/``title``, or an unbounded plan.
    """
    parsed = _role_output_dict(result, role="Planner")
    milestones = parsed.get("milestones")
    if not isinstance(milestones, list) or not milestones:
        raise ValueError("Planner output must contain a non-empty 'milestones' list")
    if len(milestones) > _MAX_PLAN_MILESTONES:
        raise ValueError(
            f"Planner plan exceeds the {_MAX_PLAN_MILESTONES}-milestone limit"
        )
    normalized: list[dict[str, Any]] = []
    for item in milestones:
        if not isinstance(item, dict):
            raise ValueError("each Planner milestone must be an object")
        milestone = {
            "id": str(item.get("id", "")).strip(),
            "title": str(item.get("title", "")).strip(),
            "scope": str(item.get("scope", "")).strip(),
            "exit_criteria": str(item.get("exit_criteria", "")).strip(),
        }
        if not milestone["id"] or not milestone["title"]:
            raise ValueError("each Planner milestone must have an id and a title")
        normalized.append(milestone)
    return normalized


def parse_evidence_verdict(result: Any, expected_fingerprint: str) -> dict[str, Any]:
    """Parse an Evidence-officer verdict, rejecting a fingerprint mismatch.

    Accepts a Claude result envelope or a raw string. The verdict must be a JSON
    object. Raises ``ValueError`` when the verdict declares ``fingerprint_mismatch``
    true, or when ``observed_fingerprint`` is present and does not equal
    ``expected_fingerprint`` — a mismatch is a hard fail for the commit gate
    (M-GV-05). Otherwise returns the verdict dict.
    """
    parsed = _role_output_dict(result, role="Evidence-officer")
    mismatch = bool(parsed.get("fingerprint_mismatch", False))
    observed = parsed.get("observed_fingerprint")
    if isinstance(observed, str) and observed and observed != expected_fingerprint:
        mismatch = True
    if mismatch:
        raise ValueError(
            "evidence fingerprint mismatch: evidence does not match the expected fingerprint"
        )
    return parsed


_SUPERVISOR_STATUSES = frozenset({"continue", "escalate", "narrow", "replace"})


def parse_supervisor_recommendations(result: Any) -> dict[str, Any]:
    """Parse Supervisor output into recommendations, surfacing no-progress.

    Accepts a Claude result envelope or a raw string. The output must be a JSON
    object with ``no_progress`` (bool), a ``status`` of ``continue``/``escalate``/
    ``narrow``/``replace``, and a ``recommendations`` list. A stalled run is
    surfaced as ``no_progress: True`` in the returned dict (the caller escalates
    via the control surface, M-CC-08). Fails closed on malformed output, an
    unknown status, or a missing recommendations list.
    """
    parsed = _role_output_dict(result, role="Supervisor")
    status = str(parsed.get("status", "")).strip()
    if status not in _SUPERVISOR_STATUSES:
        raise ValueError(
            f"Supervisor status must be one of {sorted(_SUPERVISOR_STATUSES)}"
        )
    recommendations = parsed.get("recommendations")
    if not isinstance(recommendations, list):
        raise ValueError("Supervisor output must include a 'recommendations' list")
    bounded = [str(item) for item in recommendations if str(item).strip()]
    return {
        "no_progress": bool(parsed.get("no_progress", False)),
        "status": status,
        "recommendations": bounded,
    }


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
        self._active: dict[str, asyncio.subprocess.Process] = {}

    async def _run_claude(
        self,
        command: list[str],
        project: ProjectPolicy,
        effort: Effort,
        execution_root: Path,
        text: str,
        *,
        stream: bool = False,
        execution_id: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Shared bounded-subprocess core for the drafter and the reviewer.

        Spawns the given Claude command, pipes ``text`` to stdin, bounds wall
        clock and output size, and parses the result envelope (one JSON object
        in plain ``json`` mode, or a newline-delimited event stream in
        ``stream-json`` mode). The caller shapes the public response; this
        method only reports what happened. On timeout the process group is
        killed and ``timed_out`` is reported.
        """
        try:
            before = await project_status(
                execution_root, self.settings.git_command_timeout_seconds
            )
        except Exception:
            before = {"working_tree": "", "branch": "unknown"}
        # M-CC-11 credential_policy: when a project opts in, the Claude worktree
        # launcher receives a minimal base environment plus ONLY the credential
        # env names the project declares — never a copy of the bridge's whole
        # environment. Without a credential_policy (the default), use
        # build_minimal_executor_env to project only safe minimal keys and avoid
        # leaking host secrets.
        if env is not None:
            environment = dict(env)
        elif project.credential_policy is not None:
            environment = projected_credential_env(
                project.credential_policy,
                base={
                    "PATH": os.environ.get("PATH", os.defpath),
                    "HOME": os.environ.get("HOME", ""),
                    "LANG": os.environ.get("LANG", "C.UTF-8"),
                    "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
                    "TZ": os.environ.get("TZ", "UTC"),
                    "NO_COLOR": "1",
                },
            )
        else:
            explicit_vars: dict[str, str] = {}
            if "CLAUDE_CODE_ENTRYPOINT" in os.environ:
                explicit_vars["CLAUDE_CODE_ENTRYPOINT"] = os.environ["CLAUDE_CODE_ENTRYPOINT"]
            environment = build_minimal_executor_env(
                os.environ,
                explicit_projection=explicit_vars,
                candidate_root=execution_root,
            )
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
        if execution_id is not None:
            self._active[execution_id] = process

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(text.encode("utf-8")),
                timeout=self.settings.claude_timeout_seconds,
            )
        except asyncio.TimeoutError:
            await terminate_process_group(process)
            try:
                after_timeout = await project_status(
                    execution_root, self.settings.git_command_timeout_seconds
                )
            except Exception:
                after_timeout = {"working_tree": "", "branch": "unknown"}
            return {
                "timed_out": True,
                "exit_code": process.returncode,
                "stderr": "",
                "claude_result": {},
                "before": before,
                "after": after_timeout,
                "oversized": False,
            }
        except asyncio.CancelledError:
            await terminate_process_group(process)
            raise
        finally:
            if execution_id is not None:
                self._active.pop(execution_id, None)

        try:
            after = await project_status(
                execution_root, self.settings.git_command_timeout_seconds
            )
        except Exception:
            after = {"working_tree": "", "branch": "unknown"}
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

    async def cancel(self, execution_id: str, reason: str = "cancelled_by_caller") -> bool:
        """Cancel an active Claude execution by execution_id.

        Returns True when a running process was found and terminated. This is a
        best-effort cancellation: the process may already have exited by the
        time the request is handled.
        """
        process = self._active.get(execution_id)
        if process is None or process.returncode is not None:
            return False
        await terminate_process_group(process)
        return True

    async def execute(
        self,
        project: ProjectPolicy,
        specification: str,
        effort: Effort,
        *,
        root: Path | None = None,
        resume_session_id: str | None = None,
        max_turns: int | None = None,
        execution_id: str | None = None,
        env: Mapping[str, str] | None = None,
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
            execution_id=execution_id,
            env=env,
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
        execution_id: str | None = None,
        env: Mapping[str, str] | None = None,
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
            execution_id=execution_id,
            env=env,
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


# ---------------------------------------------------------------------------
# M2.2: ExecutorPort adapter for the legacy ClaudeExecutor
# ---------------------------------------------------------------------------


class ClaudeExecutorAdapter:
    """Adapter that makes ``ClaudeExecutor`` satisfy the ``ExecutorPort`` protocol.

    The adapter translates the lifecycle-neutral ``ExecutionRequest`` into the
    legacy ``ClaudeExecutor.execute`` call shape, enforces candidate/canonical
    isolation, computes modified paths from the before/after repository status,
    and emits checkpoints so the adapter's ``supports_checkpoints`` declaration
    is honest.
    """

    _ADAPTER_ID = "claude"

    def __init__(
        self,
        executor: ClaudeExecutor | None = None,
        *,
        runner: Any = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or Settings.from_env({})
        self._executor = executor or ClaudeExecutor(self.settings)
        self._runner = runner

    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            adapter_id=self._ADAPTER_ID,
            adapter_version="0.11.0",
            supports_resume=True,
            supports_streaming=True,
            supports_cancellation=True,
            supports_checkpoints=True,
            supported_network_modes=(NetworkMode.NONE,),
            requires_container_runtime=False,
            supports_host_containment=True,
            declared_tools=("Read", "Edit", "Write", "Glob", "Grep"),
            max_concurrent_executions=1,
        )

    async def health(self) -> ExecutorHealthStatus:
        from .containment import probe_bwrap_containment

        doctor = probe_bwrap_containment()
        return ExecutorHealthStatus(
            adapter_id=self._ADAPTER_ID,
            healthy=True,
            runtime_version="0.11.0",
            containment_available=doctor.healthy,
            details={"bwrap_path": doctor.bwrap_path, "bwrap_healthy": doctor.healthy},
        )

    async def start(
        self,
        request: ExecutionRequest,
        on_checkpoint: Callable[[ExecutorCheckpoint], None] | None = None,
    ) -> ExecutionResult:
        from datetime import datetime, timezone
        from .containment import (
            build_executor_environment,
            probe_bwrap_containment,
            validate_candidate_isolation,
        )

        disallowed = {"Bash", "WebFetch", "WebSearch"}
        if any(tool in request.allowed_tools for tool in disallowed):
            raise ValueError(
                f"Denied tool requested: {sorted(set(request.allowed_tools) & disallowed)}"
            )

        if (
            request.resource_limits.network_mode
            not in self.capabilities().supported_network_modes
        ):
            raise ValueError(
                f"Unsupported network mode: {request.resource_limits.network_mode.value}"
            )

        validate_candidate_isolation(request)

        init_checkpoint = ExecutorCheckpoint(
            checkpoint_id=f"chk_{request.execution_id}_init",
            execution_id=request.execution_id,
            sequence=1,
            summary="Claude execution initiated",
            created_at=datetime.now(timezone.utc).isoformat(),
            resumable_token=request.resume_session_id,
        )
        if on_checkpoint is not None:
            on_checkpoint(init_checkpoint)

        if self._runner is not None and hasattr(self._runner, "start"):
            outcome_res = self._runner.start(request, on_checkpoint=on_checkpoint)
            if asyncio.iscoroutine(outcome_res):
                res = await outcome_res
            else:
                res = outcome_res
            if isinstance(res, ExecutionResult):
                return res

        project = self._synthetic_project(request)
        effort = self._resolve_effort(request)
        env = build_executor_environment(request)
        resume_session_id = self._resume_token(request)

        outcome = await self._executor.execute(
            project,
            request.mandate,
            effort,
            root=request.candidate_root,
            resume_session_id=resume_session_id,
            max_turns=request.resource_limits.max_turns,
            execution_id=request.execution_id,
            env=env,
        )

        doctor = probe_bwrap_containment()
        containment_profile = "bwrap" if doctor.healthy else "UNCONTAINED_TRUSTED_DEV"

        return self._build_result(
            request,
            outcome,
            containment_profile,
            init_checkpoint=init_checkpoint,
            on_checkpoint=on_checkpoint,
        )

    async def resume(
        self,
        request: ExecutionRequest,
        checkpoint: ExecutorCheckpoint,
        on_checkpoint: Callable[[ExecutorCheckpoint], None] | None = None,
    ) -> ExecutionResult:
        if self._runner is not None and hasattr(self._runner, "resume"):
            outcome_res = self._runner.resume(
                request, checkpoint, on_checkpoint=on_checkpoint
            )
            if asyncio.iscoroutine(outcome_res):
                return await outcome_res
            return outcome_res

        session_id = request.resume_session_id or checkpoint.resumable_token
        resumed_request = ExecutionRequest(
            execution_id=request.execution_id,
            project_id=request.project_id,
            job_id=request.job_id,
            candidate_root=request.candidate_root,
            mandate=request.mandate,
            canonical_root=request.canonical_root,
            allowed_tools=request.allowed_tools,
            denied_tools=request.denied_tools,
            env_projection=request.env_projection,
            resource_limits=request.resource_limits,
            resume_checkpoint_id=checkpoint.checkpoint_id,
            resume_session_id=session_id,
            metadata=request.metadata,
        )
        return await self.start(resumed_request, on_checkpoint=on_checkpoint)

    async def cancel(
        self,
        execution_id: str,
        reason: str = "cancelled_by_caller",
    ) -> bool:
        if self._runner is not None and hasattr(self._runner, "cancel"):
            res = self._runner.cancel(execution_id, reason=reason)
            if asyncio.iscoroutine(res):
                return bool(await res)
            return bool(res)
        return await self._executor.cancel(execution_id, reason=reason)

    def _synthetic_project(self, request: ExecutionRequest) -> ProjectPolicy:
        return ProjectPolicy(
            id="executor-adapter",
            display_name=request.project_id,
            description="Synthetic project for executor adapter invocation",
            root=request.candidate_root,
            capabilities=frozenset(
                {ProjectCapability.REPOSITORY_READ, ProjectCapability.CLAUDE_EDIT}
            ),
            allow_ultracode=True,
        )

    def _resolve_effort(self, request: ExecutionRequest) -> Effort:
        effort = request.metadata.get("effort")
        if effort in ("high", "ultracode"):
            return effort  # type: ignore[return-value]
        return "high"

    def _resume_token(self, request: ExecutionRequest) -> str | None:
        return request.resume_session_id

    def _build_result(
        self,
        request: ExecutionRequest,
        outcome: dict[str, Any],
        containment_profile: str,
        *,
        init_checkpoint: ExecutorCheckpoint | None = None,
        on_checkpoint: Callable[[ExecutorCheckpoint], None] | None = None,
    ) -> ExecutionResult:
        from datetime import datetime, timezone
        from .containment import compute_modified_paths

        status = outcome.get("status")
        before = outcome.get("repository_before") or outcome.get("repository") or {}
        after = outcome.get("repository_after") or outcome.get("repository") or {}
        modified_paths = compute_modified_paths(
            request.candidate_root, before, after
        )

        usage = outcome.get("usage") or {}
        input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        output_tokens = (
            usage.get("output_tokens") or usage.get("completion_tokens") or 0
        )
        resource_usage = ExecutorResourceUsage(
            turns_used=outcome.get("num_turns") or 0,
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            exit_code=outcome.get("exit_code"),
        )

        term_checkpoint = ExecutorCheckpoint(
            checkpoint_id=f"chk_{request.execution_id}_terminal",
            execution_id=request.execution_id,
            sequence=2 if init_checkpoint is not None else 1,
            summary=f"Claude execution finished with status {status}",
            created_at=datetime.now(timezone.utc).isoformat(),
            resumable_token=outcome.get("session_id") or request.resume_session_id,
            diagnostics={"status": status, "exit_code": outcome.get("exit_code")},
        )
        if on_checkpoint is not None:
            on_checkpoint(term_checkpoint)

        vendor_diagnostics: dict[str, Any] = {
            "claude": {
                "result": outcome.get("result", ""),
                "session_id": outcome.get("session_id"),
                "exit_code": outcome.get("exit_code"),
                "errors": outcome.get("errors", []),
                "stderr": outcome.get("stderr"),
            }
        }

        checkpoints = (
            (init_checkpoint, term_checkpoint)
            if init_checkpoint is not None
            else (term_checkpoint,)
        )

        if status == "timed_out":
            return ExecutionResult(
                execution_id=request.execution_id,
                adapter_id=self._ADAPTER_ID,
                state=ExecutorLifecycleState.TIMED_OUT,
                summary="Claude execution timed out",
                modified_paths=modified_paths,
                checkpoints=checkpoints,
                resource_usage=resource_usage,
                error_code="EXECUTION_TIMEOUT",
                error_message="Execution exceeded the configured timeout",
                containment_profile=containment_profile,
                vendor_diagnostics=vendor_diagnostics,
                grants_authority=False,
            )

        if status == "failed" or outcome.get("exit_code") != 0 or outcome.get("is_error"):
            message = outcome.get("error") or outcome.get("result") or "Claude execution failed"
            return ExecutionResult(
                execution_id=request.execution_id,
                adapter_id=self._ADAPTER_ID,
                state=ExecutorLifecycleState.FAILED,
                summary="Claude execution failed",
                modified_paths=modified_paths,
                checkpoints=checkpoints,
                resource_usage=resource_usage,
                error_code="CLAUDE_EXECUTION_FAILED",
                error_message=str(message)[:500],
                containment_profile=containment_profile,
                vendor_diagnostics=vendor_diagnostics,
                grants_authority=False,
            )

        return ExecutionResult(
            execution_id=request.execution_id,
            adapter_id=self._ADAPTER_ID,
            state=ExecutorLifecycleState.COMPLETED,
            summary="Claude execution completed",
            modified_paths=modified_paths,
            checkpoints=checkpoints,
            resource_usage=resource_usage,
            containment_profile=containment_profile,
            vendor_diagnostics=vendor_diagnostics,
            grants_authority=False,
        )
