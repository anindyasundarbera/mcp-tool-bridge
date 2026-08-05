# Local Development MCP v2

This is the MCPB-003 project-aware repository bridge for the ChatGPT-to-Claude
Code development workflow. ChatGPT addresses projects by stable `project_id`;
the server resolves those IDs through a local allowlist and never accepts an
arbitrary filesystem root from a tool call.

## Capabilities through MCPB-003

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
- Exclusive Markdown creation beneath configured write roots
- Hash-protected Markdown updates with exact content preservation
- Bounded unified diffs and structured before/after evidence
- One shared per-project lock for Markdown writes and Claude edits

Worktree isolation, deterministic verification, staging, and human-approved
commits remain deferred to MCPB-004 and MCPB-005.

## Repository reader tools

- `list_project_markdown`
- `search_project_markdown`
- `read_project_markdown`
- `read_project_file_range`
- `get_markdown_outline`
- `get_project_file_metadata`

## Repository writer tool

`write_project_markdown` accepts an exact `project_id`, a normalized relative
`.md` path, complete content, and one of two modes:

- `create` is the default and succeeds only when the target does not exist.
- `update` requires the SHA-256 of the current bytes and fails on a missing,
  malformed, or stale hash.

The path must be beneath a configured `markdown_write_roots` entry. Hidden,
protected, traversal, symlink, non-Markdown, missing-parent, and non-regular
targets fail closed. The tool never creates directories, deletes files, stages,
commits, invokes Claude, or runs a shell. Create uses exclusive creation;
update preserves the current mode and atomically replaces from a temporary file
in the same directory after rechecking the current bytes.

The per-project lock serializes bridge-originated Markdown writes and Claude
edits. An external process that ignores the lock can still race between the
final byte check and the filesystem replacement; the hash guard is not a
cross-process compare-and-swap guarantee.

## Markdown limits

| Setting | Default | Purpose |
|---|---:|---|
| `MAX_MARKDOWN_FILE_BYTES` | 1,048,576 | Maximum repository-reader file size |
| `MAX_MARKDOWN_LIST_RESULTS` | 500 | Maximum Markdown listing results |
| `MAX_MARKDOWN_SEARCH_RESULTS` | 100 | Maximum Markdown search results |
| `MAX_MARKDOWN_RANGE_LINES` | 1,000 | Maximum lines per range read |
| `MAX_MARKDOWN_WRITE_BYTES` | 1,048,576 | Maximum encoded content and current update size |
| `MAX_MARKDOWN_DIFF_BYTES` | 262,144 | Maximum returned unified diff size |

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
