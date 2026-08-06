# MCPB-001 — Multi-project Foundation

## Decision

Project selection is registry-driven. MCP callers provide `project_id`, never a
filesystem path. The server resolves, validates, authorizes, and locks the
project before any operation.

## Implemented tools

### `list_allowed_projects`

Returns enabled project IDs, descriptions, capabilities, and effort policy.
Absolute paths and protected-path details are not exposed.

### `get_project_status`

Returns the selected Git repository's branch, HEAD, and working-tree status.

### `send_to_local_dev`

Runs Claude Code in the selected approved project. It preserves the original
file-only tool boundary: Read, Glob, Grep, Edit, and Write are available; Bash,
web tools, and secondary MCP tools are unavailable.

## Invariants

1. Unknown, disabled, and unauthorized projects fail closed.
2. Project roots are absolute and resolved at startup.
3. Enabled Git projects must resolve to the configured repository root.
4. Two project IDs cannot resolve to the same root.
5. Self-managed projects cannot use direct `claude_edit`.
6. One modifying job may run per project; different projects may run together.
7. The chosen effort is passed exactly once to Claude Code.

## Deferred

- MCPB-002: repository reader
- MCPB-003: Markdown writer with hash concurrency
- MCPB-004: worktree-isolated development and self-update staging
- MCPB-005: deterministic verification and human-approved commits

