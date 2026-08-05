# Local Development MCP v2

This is the MCPB-001 multi-project foundation for the ChatGPT-to-Claude Code
development bridge. ChatGPT addresses projects by stable `project_id`; the
server resolves those IDs through a local allowlist and never accepts an
arbitrary filesystem root from a tool call.

## MCPB-001 capabilities

- Validated JSON project registry
- Safe public project discovery
- Exact project selection for every development action
- Per-project execution locks
- Project-aware Git observations
- Project-specific effort policy
- Existing Claude Code restrictions preserved
- OpenAI connector mTLS tunnel preserved

Repository reading, Markdown writing, worktree isolation, verification, and
commit tools are intentionally reserved for MCPB-002 through MCPB-005.

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

