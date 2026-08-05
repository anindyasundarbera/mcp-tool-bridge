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

        return cls(
            projects_config_path=config_path,
            claude_model=model,
            mcp_host=host,
            mcp_port=_positive_int(env, "MCP_PORT", 8000),
            claude_timeout_seconds=_positive_int(
                env, "CLAUDE_TIMEOUT_SECONDS", 1800
            ),
            claude_max_turns=_positive_int(env, "CLAUDE_MAX_TURNS", 30),
            max_input_chars=_positive_int(env, "MAX_INPUT_CHARS", 100000),
            max_output_bytes=_positive_int(env, "MAX_OUTPUT_BYTES", 2097152),
        )

