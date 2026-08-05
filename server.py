import asyncio
import json
import os
import signal
from pathlib import Path
from typing import Annotated, Any, Literal
from pydantic import Field

from dotenv import load_dotenv
from fastmcp import FastMCP
from mcp.types import ToolAnnotations


load_dotenv()

PROJECT_ROOT = Path(
    os.environ["PROJECT_ROOT"]
).expanduser().resolve(strict=True)

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "glm-5.2:cloud")
MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))

CLAUDE_TIMEOUT_SECONDS = int(
    os.getenv("CLAUDE_TIMEOUT_SECONDS", "1800")
)
CLAUDE_MAX_TURNS = int(os.getenv("CLAUDE_MAX_TURNS", "30"))
MAX_INPUT_CHARS = int(os.getenv("MAX_INPUT_CHARS", "100000"))
MAX_OUTPUT_BYTES = int(os.getenv("MAX_OUTPUT_BYTES", "2097152"))



# Claude effort
CLAUDE_EFFORT = os.getenv("CLAUDE_EFFORT", "ultracode")

VALID_EFFORT_LEVELS = {
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultracode",
}

if CLAUDE_EFFORT not in VALID_EFFORT_LEVELS:
    raise RuntimeError(f"Invalid CLAUDE_EFFORT: {CLAUDE_EFFORT}")


if not PROJECT_ROOT.is_dir():
    raise RuntimeError(f"Project directory does not exist: {PROJECT_ROOT}")

if not (PROJECT_ROOT / ".git").exists():
    raise RuntimeError(
        f"Configured project is not a Git repository: {PROJECT_ROOT}"
    )

# Only one Claude engineering job can modify the repository at a time.
execution_lock = asyncio.Lock()

mcp = FastMCP(
    name="CAIRN Local Development Bridge",
    instructions=(
        "This MCP server delegates approved implementation work to Claude Code "
        "running with glm-5.2:cloud through Ollama. The target repository is "
        "fixed by server configuration. Calls may modify repository files and "
        "must receive explicit user confirmation."
        "Delegates approved engineering work to Claude Code in the fixed "
        "CAIRN repository. Effort policy: use high for routine inspection, "
        "documentation, contained fixes, and straightforward implementation. "
        "Use ultracode only for complex architecture, difficult reasoning, "
        "cross-module changes, migrations, or substantial implementation. "
        "Never use ultracode for connectivity or status checks. An explicit "
        "user request overrides automatic effort selection. Calls may modify "
        "files and require user confirmation."
    ),
)


async def run_git_observation(*arguments: str) -> str:
    """Run a fixed, read-only Git observation command."""

    process = await asyncio.create_subprocess_exec(
        "git",
        *arguments,
        cwd=str(PROJECT_ROOT),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, _ = await process.communicate()
    return stdout.decode("utf-8", errors="replace").strip()


async def terminate_process_group(
    process: asyncio.subprocess.Process,
) -> None:
    """Terminate Claude and any child processes it started."""

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


@mcp.tool(
    name="send_to_local_dev",
    description=(
        "Delegate an approved architecture specification or implementation "
        "work order to Claude Code in the CAIRN Knowledge Store repository. "
        "Claude may read, create, and edit repository files. Shell commands, "
        "network tools, and additional MCP tools are disabled. This operation "
        "can modify files and requires explicit user confirmation."
        "Delegate an approved work order to Claude Code. "
        "Use effort='high' for routine analysis and implementation. "
        "Use effort='ultracode' only for complex architecture, difficult "
        "reasoning, cross-module implementation, migrations, or when the "
        "user explicitly requests Ultracode. This tool can modify files."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def send_to_local_dev(
    text: Annotated[
        str,
        Field(
            description=(
                "The complete architecture specification, implementation "
                "work order, or engineering instruction for Claude Code."
            ),
            min_length=1,
        ),
    ],
    effort: Annotated[
        Literal["high", "ultracode"],
        Field(
            description=(
                "Reasoning intensity selected from task complexity. Use high "
                "for routine work. Use ultracode for complex architecture, "
                "difficult reasoning, cross-module implementation, migrations, "
                "or substantial engineering work. Never use ultracode for "
                "connectivity tests or simple status checks. An explicit user "
                "preference takes precedence."
            )
        ),
    ] = "high",
) -> dict[str, Any]:
    """Run one controlled Claude Code engineering task."""

    specification = text.strip()

    if not specification:
        raise ValueError("text must not be empty")

    if len(specification) > MAX_INPUT_CHARS:
        raise ValueError(
            f"text exceeds the {MAX_INPUT_CHARS}-character limit"
        )

    command = [
        "ollama",
        "launch",
        "claude",
        "--model",
        CLAUDE_MODEL,
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
        str(CLAUDE_MAX_TURNS),
        "--output-format",
        "json",
        "--effort",
        CLAUDE_EFFORT,
        "--effort",
        effort,
    ]

    environment = os.environ.copy()
    environment["NO_COLOR"] = "1"

    async with execution_lock:
        before_status = await run_git_observation("status", "--short")

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(PROJECT_ROOT),
            env=environment,
            start_new_session=True,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(specification.encode("utf-8")),
                timeout=CLAUDE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            await terminate_process_group(process)

            return {
                "status": "timed_out",
                "model": CLAUDE_MODEL,
                "project": str(PROJECT_ROOT),
                "timeout_seconds": CLAUDE_TIMEOUT_SECONDS,
                "working_tree": await run_git_observation(
                    "status", "--short"
                ),
            }

        after_status = await run_git_observation("status", "--short")
        diff_stat = await run_git_observation("diff", "--stat")

    if len(stdout_bytes) > MAX_OUTPUT_BYTES:
        return {
            "status": "failed",
            "model": CLAUDE_MODEL,
            "project": str(PROJECT_ROOT),
            "error": "Claude output exceeded the configured size limit",
            "exit_code": process.returncode,
            "working_tree": after_status,
            "diff_stat": diff_stat,
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

    succeeded = (
        process.returncode == 0
        and not claude_result.get("is_error", False)
    )

    response: dict[str, Any] = {
        "status": "completed" if succeeded else "failed",
        "model": CLAUDE_MODEL,
        "project": str(PROJECT_ROOT),
        "exit_code": process.returncode,
        "result": claude_result.get("result", ""),
        "session_id": claude_result.get("session_id"),
        "terminal_reason": claude_result.get("terminal_reason"),
        "subtype": claude_result.get("subtype"),
        "errors": claude_result.get("errors", []),
        "working_tree_before": before_status,
        "working_tree_after": after_status,
        "diff_stat": diff_stat,
        "effort": effort,
    }

    if stderr:
        response["stderr"] = stderr[-4000:]

    return response


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host=MCP_HOST,
        port=MCP_PORT,
    )
