# Local Development MCP v2

This is the MCPB-004 project-aware ChatGPT-to-Claude Code development bridge.
ChatGPT addresses projects by stable `project_id`; the server resolves those
IDs through a local allowlist and never accepts an arbitrary filesystem root,
worktree path, or Claude session ID from a tool call.

## Capabilities through MCPB-004

- Validated multi-project registry and exact capability authorization
- Safe project discovery and Git status
- Bounded Markdown list, search, full read, range read, outline, and metadata
- Hash-protected Markdown create/update beneath approved write roots
- Hidden, protected, traversal, non-Markdown, non-regular, and symlink access
  denied
- One shared per-project modifying lock; independent projects remain
  independent
- Clean-baseline-gated detached Git worktree per development job
- Asynchronous job IDs, status/results, cancellation, and retained candidates
- Resume only through a bridge-created job with a bridge-captured Claude
  session
- Self-managed candidates started temporarily on a separate localhost port and
  health-checked through MCP
- Existing Claude `acceptEdits` boundary with Bash, web, secondary MCP tools,
  slash commands, and `bypassPermissions` disabled

Deterministic project verification, diff fingerprints, staging, commits,
candidate promotion, restart, push, merge, and rollback switching remain
deferred to MCPB-005 or later explicit authorization.

## MCP tools

Repository and Markdown tools:

- `list_allowed_projects`
- `get_project_status`
- `list_project_markdown`
- `search_project_markdown`
- `read_project_markdown`
- `read_project_file_range`
- `get_markdown_outline`
- `get_project_file_metadata`
- `write_project_markdown`

Isolated development lifecycle:

- `send_to_local_dev` queues a new worktree-isolated job and returns
  immediately with a 32-character job ID.
- `get_development_job` returns changing status and bounded terminal evidence.
- `resume_development_job` continues a terminal job in its retained worktree;
  callers provide the job ID, never a raw session ID.
- `cancel_development_job` stops a queued or running job while retaining its
  worktree and candidate changes.

Job states are `queued`, `preparing_worktree`, `running`, optionally
`validating_candidate`, and one of `completed`, `failed`, `timed_out`, or
`cancelled`. Job records live in memory for MCPB-004. Restarting the server
invalidates their IDs and resume authority but does not delete worktrees.

## Isolation rules

Before a new job starts, the configured checkout must be a clean Git top level,
including no staged or untracked files. The bridge records its exact `HEAD` and
creates a detached worktree beneath:

```text
JOB_WORKTREE_ROOT/<project_id>/<job_id>
```

The worktree root must be private, symlink-free, and outside every configured
project. Claude runs only in that detached worktree. Worktrees are retained for
review and the MCPB-005 verification/commit workflow; MCPB-004 exposes no
worktree deletion operation.

All modifying bridge operations for one project use the same lock, so a
Markdown write cannot overlap a Claude job. Jobs for different projects can run
independently.

## Self-managed bridge candidates

A `self_managed` project may enable `claude_edit` because development now occurs
in an isolated worktree. It may not enable direct `markdown_write` against the
production checkout.

After a successful self-managed Claude run, the bridge starts the candidate's
`server.py` with `MCP_HOST=127.0.0.1` and `MCP_PORT=CANDIDATE_MCP_PORT`, connects
through MCP, and verifies the lifecycle tool surface. The candidate process is
then stopped and its worktree retained. Candidate startup receives a minimal
allowlisted environment without tokens, certificates, ngrok settings, or
arbitrary production environment variables. A healthy result is a startup
check, not deterministic project verification, and never promotes or restarts
production.

## Limits

| Setting | Default | Purpose |
|---|---:|---|
| `CLAUDE_TIMEOUT_SECONDS` | 1,800 | Maximum Claude run time |
| `CLAUDE_MAX_TURNS` | 30 | Default Claude turn limit |
| `MAX_INPUT_CHARS` | 100,000 | Maximum job instruction length |
| `MAX_OUTPUT_BYTES` | 2,097,152 | Maximum Claude stdout |
| `MAX_MARKDOWN_FILE_BYTES` | 1,048,576 | Maximum reader file size |
| `MAX_MARKDOWN_LIST_RESULTS` | 500 | Maximum Markdown listing results |
| `MAX_MARKDOWN_SEARCH_RESULTS` | 100 | Maximum search results |
| `MAX_MARKDOWN_RANGE_LINES` | 1,000 | Maximum lines per range read |
| `MAX_MARKDOWN_WRITE_BYTES` | 1,048,576 | Maximum Markdown write/current size |
| `MAX_MARKDOWN_DIFF_BYTES` | 262,144 | Maximum returned writer diff |
| `GIT_COMMAND_TIMEOUT_SECONDS` | 30 | Maximum fixed Git command time |
| `CANDIDATE_HEALTH_TIMEOUT_SECONDS` | 30 | Candidate startup/probe deadline |

`MCP_PORT` defaults to `8000`; `CANDIDATE_MCP_PORT` defaults to `8001` and must
differ. `JOB_WORKTREE_ROOT` defaults to
`~/.local/state/local-dev-mcp/worktrees`.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
cp projects.example.json projects.json
chmod 600 .env projects.json
```

Keep the real ngrok token only in `.env`. Review `projects.json` before startup.
Only exact IDs and capabilities from that file can be used through MCP. Ensure
`JOB_WORKTREE_ROOT` is outside every configured project root.

## Run

```bash
python server.py
```

In a second terminal:

```bash
python tunnel.py
```

The connector URL remains `https://<ngrok-domain>/mcp`. The ngrok endpoint is
protected by the OpenAI connector mTLS CA; Ollama is never exposed directly.

## Test

Core tests have no network, model, ngrok, Ollama, or real-repository dependency:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

After the server is running, the separate live smoke test lists tools, checks a
project, starts an isolated no-edit job, and polls its result:

```bash
python test_client.py --project cairn-knowledge-store
```
