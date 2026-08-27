# M-GV-03 — External grounding for genesis/apex authority

## Decision

`ProjectPolicy` is the highest authority in the bridge and is self-contained in
`projects.json`: nothing requires it to be grounded in an external ratified
spec, so a project's apex authority can silently drift from whatever
(if anything) authorized it. M-GV-03 makes external grounding for genesis/apex
authority an **opt-in, capability-less** invariant:

1. `ProjectPolicy` gains an optional `apex_authority_ref` — either a URL
   (`scheme://target`) or a repo-local pin (`<relative-path>@<git-sha>`, a
   path in the repo plus the commit SHA the authority is grounded at). The
   format is validated at model time (non-empty, NUL/control-free, ≤512
   chars, no absolute/traversal path in the repo-path form, real URL scheme,
   7–40-hex commit SHA).
2. A per-policy opt-in flag `require_apex_grounding` rejects a policy that
   declares the flag without an `apex_authority_ref` (`validate_policy`). A
   bridge-wide opt-in flag `Settings.require_apex_grounding`
   (`REQUIRE_APEX_GROUNDING`, default off) makes `ProjectRegistry.load` reject
   **every** enabled policy missing an `apex_authority_ref`.
3. The registry maintains a **durable registration ledger**
   (`store.registered_apex_authority`, additive `CREATE TABLE IF NOT EXISTS`,
   `SCHEMA_VERSION` stays 1): the first `load` of a grounded project records
   its ref; a later `load` whose loaded ref differs is rejected
   ("changed without re-confirmation") unless the operator explicitly
   re-confirms via `ProjectRegistry.confirm_apex_authority`, which overwrites
   the ledger row durably. The registry is loaded against the same
   `bridge.db` the job manager uses.
4. The **genesis job** (the first job ever created for a project, detected as
   zero prior `jobs` rows for the project) requires the ref to be present and
   unchanged since registration
   (`DevelopmentJobManager._enforce_apex_grounding_for_genesis`, wired into
   `start`). A grounded project whose ref drifted is rejected before any
   runtime work begins.

## Surface

No new tool, no new capability. The flag is capability-less and disabled by
default, so projects that do not use external grounding are byte-for-byte
unaffected. `confirm_apex_authority` is an internal registry method (no MCP
surface).

## Tests

`tests/test_registry.py`: a grounded policy registers (repo-path and URL
forms); an ungrounded policy is rejected when the flag is set (global and
per-policy) and still loads when it is not; malformed ref shapes (empty,
missing `@commit`, bad SHA, traversal path, scheme-less URL) are rejected;
changing the apex ref without re-confirmation is rejected and explicit
re-confirmation accepts the new ref durably. `tests/test_jobs.py`: a matching
genesis job is accepted; a drifted project's genesis job is rejected.
`tests/test_config.py`: the `REQUIRE_APEX_GROUNDING` flag defaults off and is
truthy-set.

## Status

Implemented and verified locally. Full core suite green (449 tests).
