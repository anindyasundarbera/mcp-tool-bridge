# M-GV-05 — Self-certification prevention

## Decision

An independent-review guarantee is only meaningful if the agent that **drafted**
a candidate is not the same agent that **certifies** it. Before M-GV-05, the
bridge recorded who *reviewed* a candidate but never who *drafted* it, so a
reviewer could certify their own work undetected — a self-certification hole in
the review boundary (MCPB-007).

M-GV-05 records the drafter's identity on the job and rejects reviews by that
same identity. The change is **additive**:

- `DevelopmentJob` gains a `drafter_id` field, persisted in the additive
  `job_progress` sidecar table (new `drafter_id TEXT` column via the guarded
  `_ADDITIVE_COLUMNS` migration path; `SCHEMA_VERSION` stays 1).
- `jobs.start(..., drafter_id=...)` and `jobs.delegate(..., drafter_id=...)`
  accept an optional drafter identity; a delegated child inherits its parent's
  drafter unless overridden. `set_drafter` allows a late assignment (e.g. a
  drafter retroactively identified), and `drafter_id_for` reads it back.
- `record_candidate_review` rejects `reviewer_id == drafter_id` with a clear
  `ReviewError` ("self-certification is not allowed"), while leaving the guard
  inert when no drafter is recorded (backwards-compatible with existing review
  flows).
- The bridge's own `send_to_local_dev` entry point records `drafter_id="ChatGPT"`.

No new MCP tools, no new push surface, no schema version change — the drafter
identity is metadata that strengthens an existing governance check.

## Security boundary

The guard is fail-open by design when a drafter was never recorded (legacy jobs
and existing review flows keep working), but fail-closed once a drafter identity
is present: an independent reviewer must differ from the drafter. Identity is
opaque text (validated non-empty, ≤200 chars, printable); it is never used for
authorization, only for the self-certification comparison.
