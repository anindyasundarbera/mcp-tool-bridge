# MCPB-002 — Project-aware Repository Reader

## Decision

Repository reads are Markdown-only, project-aware, bounded, and fail closed.
Every tool resolves an exact `project_id` with the `repository_read` capability.
Tool callers provide repository-relative paths, never filesystem roots.

## Implemented tools

- `list_project_markdown`: bounded Markdown path discovery by directory depth.
- `search_project_markdown`: bounded literal, case-insensitive ripgrep search.
- `read_project_markdown`: complete UTF-8 Markdown with content and metadata.
- `read_project_file_range`: bounded 1-based ranges with optional SHA-256 guard.
- `get_markdown_outline`: ATX and setext headings outside fenced code blocks.
- `get_project_file_metadata`: SHA-256, size, lines, timestamp, and Git status.

All six tools are annotated read-only, non-destructive, and closed-world.

## Security invariants

1. Unknown, disabled, and unauthorized projects fail closed in the registry.
2. Paths must be normalized and relative; absolute paths and traversal fail.
3. Hidden paths and configured protected paths cannot be read or searched.
4. Only UTF-8 `.md` files can be returned.
5. Symlink components are rejected, even when their targets remain in-project.
6. Resolved paths must remain beneath the configured project root.
7. Full reads, ranges, listings, searches, query length, and excerpts are bounded.
8. Search invokes `rg` with a fixed argument vector and never uses a shell.
9. Responses expose repository-relative paths, never absolute project roots.
10. No reader tool writes files, modifies Git, or invokes Claude Code.

## Default limits

| Setting | Default |
|---|---:|
| `MAX_MARKDOWN_FILE_BYTES` | 1,048,576 |
| `MAX_MARKDOWN_LIST_RESULTS` | 500 |
| `MAX_MARKDOWN_SEARCH_RESULTS` | 100 |
| `MAX_MARKDOWN_RANGE_LINES` | 1,000 |

## Deferred

- MCPB-003: Markdown writer with SHA-256 concurrency control
- MCPB-004: worktree-isolated development and self-update staging
- MCPB-005: deterministic verification and human-approved commits
