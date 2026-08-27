# MCPB-CC-07 — Read-only completed-worktree recovery

- **Status:** DONE
- **Milestone:** M-CC-07 — Read-only completed-worktree recovery
- **Tool surface:** none (internal only; no new MCP tool, capability, or schema)
- **Version:** 0.10.2

## Accepted scope

Re-attach a vanished completed-job worktree from the content-addressed candidate
so completed work remains recoverable read-only. The candidate is the immutable
detached commit the bridge created during `commit_verified_development_job`
(`DevelopmentJob.committed_sha`); its verified fingerprint, base head, and
ordered path list come from the verification record.

## Decision

Add `GitWorktreeManager.recover_completed_worktree(
project, job_id, committed_sha, base_head, paths, expected_fingerprint)` in
`src/local_dev_mcp/worktrees.py`. The method:

1. Recreates the job's isolated worktree as a **detached** checkout of
   `committed_sha` at the original job path (`git worktree prune` first clears
   the stale registration left by the vanished directory; `git worktree add
   --detach` registers the new checkout without creating any branch or moving
   any ref).
2. Re-derives the candidate fingerprint from the committed object via
   `verification.committed_state` (lazy import, breaking the
   verification<->worktrees module cycle) and fails closed unless it equals the
   verification record's `diff_fingerprint`.
3. Returns the worktree with `JobWorktree.read_only = True` (an additive
   dataclass field; all existing constructions are untouched). The canonical
   checkout is never written and its HEAD must not move during recovery.

Security: fixed argument vectors only, no shell; the recreated worktree stays
beneath the trusted private worktree root; `job_id` is validated as a single
safe path component and `committed_sha` as a 40-hex SHA-1; a mismatch or any
error removes the partially-created checkout.

## Tests

`tests/test_worktrees.py::CompletedWorktreeRecoveryTests`:

- a vanished completed worktree is recovered: HEAD equals `committed_sha`, no
  branch is created, the working tree is clean, the re-derived `committed_state`
  fingerprint matches the verified fingerprint, `read_only` is true, and the
  canonical checkout's HEAD/refs are untouched;
- a mismatched fingerprint is rejected and the partial checkout is cleaned up
  (no leftover registration);
- an existing (non-vanished) worktree path is refused;
- invalid `job_id` / `committed_sha` values are refused.

## Acceptance

Completed work is recoverable read-only; no new tool surface.
