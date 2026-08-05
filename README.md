# Local Development MCP v2

This is the MCPB-002 project-aware repository reader for the ChatGPT-to-Claude Code
development bridge. ChatGPT addresses projects by stable `project_id`; the
server resolves those IDs through a local allowlist and never accepts an
arbitrary filesystem root from a tool call.

## Capabilities through MCPB-002

- Validated JSON project registry
- Safe public project discovery
- Exact project selection for every development action
- Per-project execution locks
- Project-aware Git observations
- Project-specific effort policy
- Existing Claude Code restrictions preserved
- OpenAI connector mTLS tunnel preserved
- Bounded Markdown discovery and literal text search
- Full-file and line-range Markdown reads
- Markdown outlines that ignore fenced code blocks
- SHA-256, file size, line count, timestamp, and per-file Git status
- Hidden, protected, non-Markdown, traversal, and symlink access denied

Markdown writing, worktree isolation, verification, and commit tools are
intentionally reserved for MCPB-003 through MCPB-005.

## Repository reader tools

- `list_project_markdown`
- `search_project_markdown`
- `read_project_markdown`
- `read_project_file_range`
- `get_markdown_outline`
- `get_project_file_metadata`

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
Only exact IDs from that file can be used through MCP.

## Run

```bash
python server.py
```

In a second terminal:

```bash
python tunnel.py
```

The MCP URL remains `https://<ngrok-domain>/mcp`.

## Test

Core tests have no network or model dependency:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

After the server is running, perform the live smoke test:

```bash
python test_client.py --project cairn-knowledge-store
```

## Self-managed bridge project

`local-dev-mcp` is shipped disabled. Before enabling it:

1. Initialize the bridge directory as a Git repository.
2. Ignore `.env`, `.venv/`, caches, and local certificates.
3. Commit a known-good baseline.
4. Keep `claude_edit` disabled until MCPB-004 adds isolated worktrees.

The running production checkout must not edit or restart itself directly.
