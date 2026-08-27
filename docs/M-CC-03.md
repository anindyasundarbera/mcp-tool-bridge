# M-CC-03 — Successor job from immutable completed candidate

## Decision

A development job can currently branch only off the canonical HEAD (or a
fetched `origin/<default>` tip). To build the next candidate on top of an
already-published result, an orchestrator has no safe primitive: resuming the
completed job is impossible (completed/committed jobs are non-resumable), and
starting a fresh job from canonical HEAD loses the published work. M-CC-03 adds
an **internal-first** `create_successor_job(parent_job_id, instructions)`
method: it creates a NEW job whose worktree is pinned to the parent job's
published commit — `CommittedCandidate.committed_sha` — rather than the
canonical HEAD.

The parent stays immutable: `create_successor_job` never mutates it, and the
existing `resume` path already rejects completed/committed jobs, so no path can
alter a completed candidate. The parent/child link reuses the existing M-GV-06
`parent_job_id` field (no new ledger). **No new MCP tool** — this is internal
surface for the orchestration layer only, so the tool-count canary stays 32.

## Mechanics

- `jobs.create_successor_job(project, parent_job_id, text, effort=None)`:
  validates the parent is `completed`, has a `committed_sha` (a published
  commit to branch from), and retains its worktree; creates a new
  `DevelopmentJob` with `parent_job_id=parent_job_id` and
  `successor_base_head=parent.committed_sha`; records an authority event; and
  launches it like any other job.
- `worktrees.create_at_commit(project, job_id, base_head)`: creates a detached
  worktree pinned to an **exact local commit** in the shared object store. It
  rejects short SHAs, branch names, and missing commits (`git rev-parse
  --verify <sha>^{commit}`, fail closed); requires a clean canonical checkout;
  verifies the checkout did not change during creation; and cleans up a
  partially-created worktree on failure. Linked worktrees share the canonical
  repository's object store, so a commit made in the parent's job worktree is
  resolvable when the successor's worktree is created.
- Persistence is additive: `successor_base_head` is a `job_progress` sidecar
  column (`CREATE TABLE IF NOT EXISTS` + `_ADDITIVE_COLUMNS` migration),
  round-tripped via `_progress_to_row` / `_apply_progress_row`. SCHEMA_VERSION
  stays 1. It is surfaced in the terminal snapshot as `successor_base_head`.
- M-GV-06 scope enforcement gates on `parent_job_id` so legacy delegated
  children with a NULL mandate still fail closed. A successor also carries
  `parent_job_id` but is NOT a bounded delegated child; it is disambiguated by
  `successor_base_head` (set for successors, None for delegated children) and
  exempted from the mandate scope check.

## Scope boundary

- Successor jobs are ordinary development jobs in every other respect: same
  effort resolution, worktree isolation, budget/checkpoint tracking, and
  terminal report. Only the base commit differs.
- No merge/tag/release/deploy surface is added (see M-CC-04 for ratification).
- Internal first: the MCP tool surface is unchanged.
