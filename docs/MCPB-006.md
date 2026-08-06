# MCPB-006 — Durable job and verification state

## Decision

Job and verification state that previously lived only in process memory is now
persisted durably to a single local SQLite database beneath the bridge-owned,
private `.state` directory under `JOB_WORKTREE_ROOT`:

```text
JOB_WORKTREE_ROOT/.state/bridge.db   # 0o600, WAL, synchronous=FULL
```

`sqlite3` is Python stdlib, so this adds no dependency. The database holds three
tables — `schema_meta`, `jobs`, and `verifications` — spoken to in plain dict
rows by a focused `store` module. The `jobs` and `verification` managers own the
object-to-row mapping; `store` imports only `worktrees`, keeping the dependency
direction one-way and avoiding an import cycle.

On startup each manager re-hydrates its records synchronously in `__init__`,
before the event loop starts, then persists on every mutation as one ACID
transaction inside the existing per-manager state lock. No MCP tool, tool
parameter, schema, or safety annotation changes. Persistence is an internal
side-effect of existing mutations.

## Reconciliation

A job that was not terminal when the previous process died is reloaded as
`failed` with error `bridge restarted while job was in progress`; its asyncio
task and Claude process are gone and cannot be reattached. A terminal job whose
retained worktree directory has since vanished is reloaded as `failed` with
error `job worktree no longer exists` and `resume_available=false`. No new
status enum value is introduced. Completed jobs with a surviving worktree and a
bridge-captured Claude `session_id` remain resumable across restarts, and
single-use verification IDs remain valid for a later commit. Unparseable rows
(unknown status/effort, missing required fields) are skipped so one corrupt
record never blocks startup. A stored schema version newer than this code
supports is refused on open to avoid silent data loss.

## Security

No exposure widening: the Claude `session_id` moves from process memory to a
`0o600` SQLite file under a `0o700` directory under the already-`0o700` worktree
root, readable only by the same OS user. No new reader is introduced.
`result`, `candidate`, and `error` are redacted before they reach the store.
The only raw absolute path persisted is the retained worktree path, required to
re-adopt the worktree; it is `0o600` and never tool-exposed. Full base-HEAD and
integrity re-validation still happens at use time through the unchanged
`validate_resume`. "Bridge-created session resume only" is not weakened: callers
still pass `job_id`, never `session_id`.

## Explicitly excluded

- Auto-pruning of orphaned worktrees (manual `git worktree remove --force` only).
- An advisory `flock`/exclusive-locking guard against two bridge processes on
  one `JOB_WORKTREE_ROOT` (deferred; WAL serializes writes; the single-instance
  assumption is documented).
- Any new MCP tool, permission broadening, or tool schema change.
- Schema migration tooling beyond the `schema_meta` version seam (v1 only).