from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def _positive_int(environment: Mapping[str, str], name: str, default: int) -> int:
    raw = environment.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return value


def _positive_float(environment: Mapping[str, str], name: str, default: float) -> float:
    raw = environment.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return value


def _port(environment: Mapping[str, str], name: str, default: int) -> int:
    value = _positive_int(environment, name, default)
    if value > 65535:
        raise RuntimeError(f"{name} must be at most 65535")
    return value


def _truthy(environment: Mapping[str, str], name: str) -> bool:
    return environment.get(name, "").strip() in ("1", "true", "yes", "on")


def _absolute_path(environment: Mapping[str, str], name: str, default: str) -> Path:
    path = Path(environment.get(name, default)).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


@dataclass(frozen=True, slots=True)
class Settings:
    projects_config_path: Path
    claude_model: str
    mcp_host: str
    mcp_port: int
    claude_timeout_seconds: int
    claude_max_turns: int
    max_input_chars: int
    max_output_bytes: int
    max_markdown_file_bytes: int
    max_markdown_list_results: int
    max_markdown_search_results: int
    max_markdown_range_lines: int
    max_markdown_write_bytes: int
    max_markdown_diff_bytes: int
    job_worktree_root: Path
    git_command_timeout_seconds: int
    candidate_mcp_port: int
    candidate_health_timeout_seconds: int
    verification_command_timeout_seconds: int
    max_verification_output_bytes: int
    max_commit_message_chars: int
    verification_deterministic_repetitions: int
    claude_soft_turns: int
    claude_extension_turns: int
    claude_max_extensions: int
    claude_hard_turns: int
    claude_max_no_progress_turns: int
    claude_max_tool_failures: int
    claude_checkpoint_interval_seconds: float
    claude_checkpoint_max: int
    claude_stream_json: bool
    max_delegations_per_job: int
    reader_injection_defense: bool

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environment is None else environment
        config_path = Path(env.get("PROJECTS_CONFIG_PATH", "projects.json")).expanduser()
        if not config_path.is_absolute():
            config_path = (Path.cwd() / config_path).resolve()

        model = env.get("CLAUDE_MODEL", "glm-5.2:cloud").strip()
        host = env.get("MCP_HOST", "127.0.0.1").strip()
        if not model:
            raise RuntimeError("CLAUDE_MODEL must not be empty")
        if not host:
            raise RuntimeError("MCP_HOST must not be empty")

        mcp_port = _port(env, "MCP_PORT", 8000)
        candidate_mcp_port = _port(env, "CANDIDATE_MCP_PORT", 8001)
        if candidate_mcp_port == mcp_port:
            raise RuntimeError("CANDIDATE_MCP_PORT must differ from MCP_PORT")

        return cls(
            projects_config_path=config_path,
            claude_model=model,
            mcp_host=host,
            mcp_port=mcp_port,
            claude_timeout_seconds=_positive_int(
                env, "CLAUDE_TIMEOUT_SECONDS", 1800
            ),
            claude_max_turns=_positive_int(env, "CLAUDE_MAX_TURNS", 30),
            max_input_chars=_positive_int(env, "MAX_INPUT_CHARS", 100000),
            max_output_bytes=_positive_int(env, "MAX_OUTPUT_BYTES", 2097152),
            max_markdown_file_bytes=_positive_int(
                env, "MAX_MARKDOWN_FILE_BYTES", 1048576
            ),
            max_markdown_list_results=_positive_int(
                env, "MAX_MARKDOWN_LIST_RESULTS", 500
            ),
            max_markdown_search_results=_positive_int(
                env, "MAX_MARKDOWN_SEARCH_RESULTS", 100
            ),
            max_markdown_range_lines=_positive_int(
                env, "MAX_MARKDOWN_RANGE_LINES", 1000
            ),
            max_markdown_write_bytes=_positive_int(
                env, "MAX_MARKDOWN_WRITE_BYTES", 1048576
            ),
            max_markdown_diff_bytes=_positive_int(
                env, "MAX_MARKDOWN_DIFF_BYTES", 262144
            ),
            job_worktree_root=_absolute_path(
                env,
                "JOB_WORKTREE_ROOT",
                "~/.local/state/local-dev-mcp/worktrees",
            ),
            git_command_timeout_seconds=_positive_int(
                env, "GIT_COMMAND_TIMEOUT_SECONDS", 30
            ),
            candidate_mcp_port=candidate_mcp_port,
            candidate_health_timeout_seconds=_positive_int(
                env, "CANDIDATE_HEALTH_TIMEOUT_SECONDS", 30
            ),
            verification_command_timeout_seconds=_positive_int(
                env, "VERIFICATION_COMMAND_TIMEOUT_SECONDS", 600
            ),
            max_verification_output_bytes=_positive_int(
                env, "MAX_VERIFICATION_OUTPUT_BYTES", 1048576
            ),
            max_commit_message_chars=_positive_int(
                env, "MAX_COMMIT_MESSAGE_CHARS", 500
            ),
            verification_deterministic_repetitions=_positive_int(
                env, "VERIFICATION_DETERMINISTIC_REPETITIONS", 1
            ),
            claude_soft_turns=_positive_int(env, "CLAUDE_SOFT_TURNS", 30),
            claude_extension_turns=_positive_int(
                env, "CLAUDE_EXTENSION_TURNS", 30
            ),
            claude_max_extensions=_positive_int(
                env, "CLAUDE_MAX_EXTENSIONS", 3
            ),
            claude_hard_turns=_positive_int(env, "CLAUDE_HARD_TURNS", 120),
            claude_max_no_progress_turns=_positive_int(
                env, "CLAUDE_MAX_NO_PROGRESS_TURNS", 6
            ),
            claude_max_tool_failures=_positive_int(
                env, "CLAUDE_MAX_TOOL_FAILURES", 5
            ),
            claude_checkpoint_interval_seconds=_positive_float(
                env, "CLAUDE_CHECKPOINT_INTERVAL_SECONDS", 30
            ),
            claude_checkpoint_max=_positive_int(
                env, "CLAUDE_CHECKPOINT_MAX", 500
            ),
            claude_stream_json=_truthy(env, "CLAUDE_STREAM_JSON"),
            max_delegations_per_job=_positive_int(
                env, "MAX_DELEGATIONS_PER_JOB", 3
            ),
            reader_injection_defense=_truthy(env, "READER_INJECTION_DEFENSE"),
        )
