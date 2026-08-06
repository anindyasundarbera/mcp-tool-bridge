# Migration from the single-project bridge

This migration preserves the existing ngrok connector while replacing only the
local MCP server process.

## 1. Prepare v2 without secrets

```bash
cd ~/local-dev-mcp-v2
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
cp projects.example.json projects.json
```

Copy the existing ngrok token into the new `.env`; do not copy it into any
tracked file. Copy the public OpenAI connector CA file into the v2 directory.

## 2. Validate configuration

Confirm every enabled root exists and is the top level of its Git repository:

```bash
git -C /absolute/project/root rev-parse --show-toplevel
```

Keep `local-dev-mcp` disabled until it has a Git baseline. Direct self-editing
remains prohibited until MCPB-004 introduces isolated worktrees.

## 3. Verify offline

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

## 4. Switch the local server

Leave the existing ngrok tunnel running. Stop only the old `server.py`, then
start v2 on the same port:

```bash
python server.py
```

If startup fails, stop v2 and restart the old server. The tunnel remains bound
to the same local port, so the public connector URL need not change.

## 5. Refresh ChatGPT metadata

Refresh the connector so ChatGPT discovers:

- `list_allowed_projects`
- `get_project_status`
- project-aware `send_to_local_dev`

Run `test_client.py --project cairn-knowledge-store`, then confirm the selected
repository remains clean.

