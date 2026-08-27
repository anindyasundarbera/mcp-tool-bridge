# M-GV-07 — Parallel verify + independent review on completion

## Decision

A candidate is only end-to-end governed when the verification gate **and** the
independent review both run and both leave durable records. Before this
milestone they were manual round-trips: a project operator had to call
`verify_development_job` and then `run_independent_review` by hand, and a
completed job could sit unverified/unreviewed indefinitely.

M-GV-07 makes the pair run automatically and concurrently for projects that opt
in. The change is **additive** and opt-in:

- `ProjectPolicy` gains `parallel_gates: bool = False` (default off, so
  existing projects keep their current manual behavior; `test_no_verification_row_omits_validation_evidence`
  still holds because a non-opted-in job is untouched).
- When `parallel_gates` is enabled, `DevelopmentJobManager._run` — once a job
  reaches `COMPLETED`, the task is cleared, and the per-project lock has been
  released — schedules `VerificationCommitManager.verify` and
  `ReviewManager.run_independent_review` together with `asyncio.gather`, then
  persists both durable records.
- The two managers are **lazily constructed** (`_verification_manager` /
  `_review_manager`) inside `DevelopmentJobManager` with lazy imports to break
  the `verification`/`review` → `jobs` module cycle and to avoid opening extra
  DB connections for projects that never enable the flag.
- The gate is **non-fatal**: a failed gate never fails a job that already
  reached `COMPLETED`; the outcome is summarized in a `parallel_gates` audit
  event (status `both_recorded`/`partial`, per-gate `ok`/`failed`, bounded
  error detail).

The gate must run **outside** the `_run` project-lock block: both `verify` and
`run_independent_review` re-acquire that per-project lock internally, and
`asyncio.Lock` is not reentrant — running them inside the block would deadlock.
It also runs only after `job.task = None` because both gates require an
inactive completed job (`verification_worktree` precondition).

## Security boundary

The review half is the existing bridge-spawned independent review: a fresh,
read-only reviewer session with no `--resume`, bounded verdict, candidate drift
detection, and the bridge-spawned independence class. It still requires
`allow_bridge_reviewer`. The verification half is the existing bounded command
vector. No new tools, no new push surface, no mutation outside the existing
retained worktree.
