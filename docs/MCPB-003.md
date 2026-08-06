# MCPB-003 — Hash-protected Markdown Writer

## Decision

The bridge exposes one confirmation-gated `write_project_markdown` tool for
complete UTF-8 Markdown file creation and update. Every request resolves an
exact `project_id` with the `markdown_write` capability. Tool callers provide a
repository-relative path, never a filesystem root.

## Contract

```text
write_project_markdown(
    project_id,
    path,
    content,
    write_mode="create",
    expected_sha256=None,
)
```

- `create` is exclusive and forbids `expected_sha256`.
- `update` requires a 64-character hexadecimal SHA-256 matching the current
  bytes.
- Content is encoded as UTF-8 and preserved exactly, including line endings and
  the presence or absence of a final newline.
- Parent directories must already exist.
- Success returns before/after hashes, byte and line counts, Git status, a
  bounded repository-labelled unified diff, and an explicit truncation flag.

## Security invariants

1. The registry authorizes the exact project and `markdown_write` capability.
2. The target must be a normalized relative `.md` path beneath a configured
   `markdown_write_roots` entry.
3. Absolute, dot-segment, empty-segment, traversal, hidden, protected, and
   symlink paths fail closed.
4. Updates accept only existing regular UTF-8 Markdown files; creates never
   overwrite an existing filesystem object.
5. Encoded content and the current update target are bounded by
   `MAX_MARKDOWN_WRITE_BYTES`.
6. Create uses exclusive creation with a conservative non-executable mode.
7. Update writes a same-directory temporary file, preserves the target mode,
   flushes it, rechecks the current bytes, and atomically replaces the target.
8. Temporary files are removed on failure.
9. Markdown writes and `send_to_local_dev` share the same per-project modifying
   lock; independent projects retain independent locks.
10. Self-managed bridge projects cannot enable direct modifying capabilities
    before MCPB-004 worktree isolation.
11. Responses never expose absolute project roots.
12. The writer does not delete, create directories, stage, commit, invoke
    Claude, execute a shell, or access the web.

The bridge lock serializes bridge-originated modifications only. An external
process that ignores it can still race between the final unchanged-state check
and atomic replacement, so the implementation does not claim a cross-process
compare-and-swap guarantee.

## Limits

| Setting | Default |
|---|---:|
| `MAX_MARKDOWN_WRITE_BYTES` | 1,048,576 |
| `MAX_MARKDOWN_DIFF_BYTES` | 262,144 |

Both values must be positive integers.

## MCP metadata

The tool is marked read-write, destructive, non-idempotent, and closed-world.
Its schema exposes the `create`/`update` enum, `create` default, and conditional
hash requirement in the parameter descriptions.

## Deferred

- MCPB-004: worktree-isolated jobs, clean-baseline checks, job lifecycle, and
  safe self-update candidates
- MCPB-005: deterministic verification, diff fingerprints, exact staging, and
  human-approved commits
- Directory creation, deletion, non-Markdown writes, unrestricted Bash, push,
  merge, tag, restart, and production self-edit
