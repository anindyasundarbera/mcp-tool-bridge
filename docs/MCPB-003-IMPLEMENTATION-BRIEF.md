# MCPB-003 — Hash-protected Markdown Writer

## Objective

Add one project-aware MCP tool that creates or updates UTF-8 Markdown files
inside explicitly approved repository roots. The writer must preserve the
multi-project authorization boundary introduced by MCPB-001 and reuse the path
confinement principles implemented by the MCPB-002 Repository Reader.

This milestone does not add deletion, arbitrary file writes, shell access,
commits, worktrees, server self-restarts, or unrestricted filesystem access.

## Tool contract

Expose one tool:

```text
write_project_markdown(
    project_id,
    path,
    content,
    write_mode="create",
    expected_sha256=None,
)
```

### Parameters

| Parameter | Contract |
|---|---|
| `project_id` | Exact ID returned by `list_allowed_projects`; never a filesystem path or fuzzy name. |
| `path` | Normalized project-relative `.md` path beneath one configured `markdown_write_roots` entry. |
| `content` | Complete UTF-8 Markdown file content. Preserve it exactly; do not normalize line endings or append a newline. |
| `write_mode` | Enum: `create` or `update`; default `create`. |
| `expected_sha256` | Required for `update`, forbidden for `create`; lowercase or uppercase 64-character hexadecimal SHA-256 of the current bytes. |

### Mode behavior

- `create` succeeds only when the target does not exist.
- `update` succeeds only when the target is an existing regular Markdown file
  and its current bytes match `expected_sha256`.
- A stale or missing hash fails without modifying the target.
- The parent directory must already exist. MCPB-003 does not create directories.

### Successful response

Return structured evidence without exposing absolute local paths:

```json
{
  "project_id": "cairn-knowledge-store",
  "path": "docs/work-orders/CKS-WO-004.md",
  "write_mode": "create",
  "before_sha256": null,
  "after_sha256": "...",
  "size_bytes": 1234,
  "line_count": 42,
  "git_status": "?? docs/work-orders/CKS-WO-004.md",
  "diff": "--- /dev/null\n+++ b/docs/work-orders/CKS-WO-004.md\n...",
  "diff_truncated": false
}
```

For updates, include both before and after hashes. Do not return the complete
file content separately. Bound the returned unified diff and report truncation
explicitly.

## Authorization and path invariants

1. Resolve `project_id` with `ProjectCapability.MARKDOWN_WRITE` before touching
   the filesystem.
2. Never accept an arbitrary project root through tool input.
3. Accept only normalized relative paths. Reject absolute paths, `.` segments,
   `..`, empty segments, hidden path components, and non-`.md` targets.
4. Reject every configured `protected_paths` target and descendant.
5. Require the target to be within at least one configured
   `markdown_write_roots` entry.
6. Reject symlink components, including the target and parent path.
7. Require the resolved parent to remain beneath the configured project root.
8. Reject directories, devices, sockets, and other non-regular targets.
9. Enforce a configured encoded-byte limit before writing.
10. Do not delete, stage, commit, invoke Claude, run a shell, or access the web.

The writer and `send_to_local_dev` must share the existing per-project lock so
two bridge-originated modifications cannot overlap in the same project. Reads
remain read-only and need not take the modifying lock.

`expected_sha256` is optimistic concurrency protection for MCP-originated and
observed external changes. Re-check the existing bytes immediately before the
atomic replacement. Document that processes which ignore the bridge lock can
still race at the filesystem boundary; do not claim a stronger cross-process
compare-and-swap guarantee than the implementation provides.

## Write mechanics

- Encode `content` as UTF-8 and validate the encoded size before mutation.
- For `create`, use exclusive creation semantics so an existing path is never
  overwritten.
- For `update`, read and hash the current bytes, validate the expected hash,
  write a temporary file in the same directory, flush it, and atomically
  replace the target only after a final unchanged-state check.
- Preserve the existing file mode on update. Use a conservative regular-file
  mode for create; do not inherit executable bits.
- Clean up temporary files on all failures.
- Generate a unified diff from the before and after text using repository-
  relative labels. Diff generation must not invoke a shell.
- Query Git status only after a successful write.

If shared path logic is extracted from `reader.py`, keep reader behavior and
error messages compatible unless a test demonstrates a necessary correction.

## Configuration

Add explicit settings rather than silently reusing unrelated output limits:

```dotenv
MAX_MARKDOWN_WRITE_BYTES=1048576
MAX_MARKDOWN_DIFF_BYTES=262144
```

Both must be positive integers and documented in `.env.example`, `README.md`,
and the MCPB-003 design document. Increment the package version from `0.2.0` to
`0.3.0`.

## MCP metadata

The tool description must explain create/update semantics and the mandatory
update hash. Suggested annotations:

```python
ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)
```

The tool can replace repository content, so it must remain confirmation-gated.
It is closed-world because it only affects an authorized local project.

## Required tests

Keep all MCPB-001 and MCPB-002 tests passing and add coverage for:

- successful create beneath an approved write root;
- create refusal when the target exists;
- successful update with the current SHA-256;
- refusal of missing, malformed, and stale update hashes;
- unchanged target after every rejected request;
- rejection outside `markdown_write_roots`;
- rejection of unknown/disabled projects and projects without
  `markdown_write`;
- absolute, traversal, hidden, protected, non-Markdown, and symlink paths;
- missing parent directory and non-regular targets;
- encoded-byte size limit;
- exact content preservation;
- before/after hashes, Git status, unified diff, and diff truncation;
- temporary-file cleanup after failure;
- serialization with the same per-project lock used by Claude execution;
- FastMCP parameter schema, enum/default/hash description, and safety
  annotations.

Use temporary Git repositories. Core tests must not require ngrok, Ollama,
Claude, the network, or the user's real repositories.

## Acceptance criteria

- `PYTHONPATH=src python -m unittest discover -s tests -v` passes.
- The package builds and installs in an isolated environment.
- FastMCP lists the writer with the expected schema and annotations.
- No response leaks an absolute project root.
- Archive/source audit contains no `.env`, credentials, certificate, virtual
  environment, cache directory, or stale build output.
- Documentation states that MCPB-004 worktree isolation and MCPB-005
  verification/commit remain deferred.

## Explicitly deferred

- Directory creation and file deletion
- Non-Markdown artifact uploads or chunked uploads
- Asynchronous Claude jobs and session resume
- Per-job Git worktrees and safe self-updates (MCPB-004)
- Deterministic verification, diff fingerprints, staging, and human-approved
  commits (MCPB-005)
- Unrestricted Bash or `bypassPermissions`
