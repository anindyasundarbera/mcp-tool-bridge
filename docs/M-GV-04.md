# M-GV-04 — Spec-gated runtime under a ratified spec commit

## Decision

Runtime implementation work was allowed on any project with no named ratified
spec commit: nothing pinned a job's spec to an immutable, ratified point, so
implementation could drift from the governing spec. M-GV-04 makes runtime
spec-gating an **opt-in, capability-less** invariant:

1. `ProjectPolicy` gains an optional `ratified_spec_commit` — a full 40-hex git
   SHA pinning the project's governing spec (the apex authority) to one
   immutable commit. The format is validated at model time.
2. When set, `DevelopmentJobManager.start` (the create path) and `delegate`
   (the child-job path) require the job's spec/instructions to be **pinned to
   that commit**: the ratified SHA must appear in the spec text as a standalone
   hex token. A spec that references no commit, or a different commit, is
   rejected as **spec drift** with a clear error before any runtime work
   begins. A project that never ratified a spec commit is unaffected (opt-in).
3. **Composition with M-GV-03:** the apex authority is the spec; the ratified
   commit is the pinned point. A grounded project's genesis job must satisfy
   both checks: the external apex ref unchanged since registration AND the
   spec pinned to the ratified commit.

## Surface

No new tool, no new capability, no new schema table (the field is policy JSON
only, validated at model load). Disabled by default.

## Tests

`tests/test_jobs.py`: a job under a ratified spec (text pinned to the ratified
SHA) is accepted; a spec drift (no pin, or pinned to a different commit) is
rejected. `tests/test_config.py` (`GroundingAndSpecGatePolicyTests`): the
ratified spec commit must be a full 40-hex SHA (short, non-hex, and empty
values rejected); a valid value is accepted; the M-GV-03 per-policy grounding
flag requires a ref and accepts one.

## Status

Implemented and verified locally. Full core suite green (455 tests).
