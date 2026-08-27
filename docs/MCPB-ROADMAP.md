# BridgeLab capability roadmap

> The single document a new session writes code from. It audits every
> capability ChatGPT/Cairn asked for against the **real** codebase, says what is
> closed and what remains, and turns each unclosed gap into an implementable
> milestone with files / approach / schema / tests / acceptance.
>
> Audit method: 12 parallel read-only agents verified all 149 capability items
> against the source (file:symbol evidence below). The verdict-summary table is
> reconciled against merged `main` as of the 2026-08-28 release integration:
> **139 DONE, 0 PARTIAL, 10 MISSING** (the ten MISSING are all P3 items
> deferred as the M-IN-01 non-goal). The three former PARTIAL items —
> deterministic replay, test-kind coverage, and static analysis/schema
> validation — were closed on 2026-08-28 (`replay_verification` tool,
> `VerificationCommand.kind`, and the internal `static_analysis` block) and are
> now DONE in the table. The 2026-08-26 true status audit below is the
> authoritative milestone-by-milestone record.
>
> Per-phase decision records live in `docs/MCPB-0NN.md` +
> `docs/MCPB-0NN-IMPLEMENTATION-BRIEF.md`. `AGENTS.md`'s "Current resume pointer"
> names the phase pending acceptance. The established patterns (additive
> `CREATE TABLE IF NOT EXISTS` tables, `SCHEMA_VERSION` stays 1, no `ALTER
> TABLE` without a sidecar table, lazy imports to break the `verification↔jobs`
> cycle, `unittest` with fake executors, and the zero-new-tool-surface
> discipline) and the **non-negotiable security boundaries** in `AGENTS.md` apply
> to every milestone below.

## Current state (2026-08-26)

- **MCPB-001 → MCPB-012 and M-AL-01 have all shipped** (see `git log` and
  `docs/README.md` for the milestone index). The 149-item verdict table below
  is the original point-in-time audit reconciled against merged `main` as of
  the 2026-08-27 integration; the **2026-08-26 true status audit** (added
  after the verdict summary) remains the authoritative
  milestone-by-milestone record.
- **Major gap-closure work landed after the original audit**, much of it
  partially: the M-CT continuity tail (`8d863c7`), M-EV-01 requirement
  traceability (`4596ceb`), M-EV-05 authority records (`b09b1a8`), M-EV-06
  evidence bundle (`9422774`), M-GV-01 authorization objects (`1163a3a`),
  M-GV-06 delegation (`16fd2e7`), M-GV-07 review reconciliation (`1a8bfe4`),
  M-CC-01/02/05 draft PR (`f80c05a`), M-CC-09 hermetic lab (`7186368`), and
  M-CC-11 in three sub-commits (prompt-injection defense `b5ed195`,
  supply-chain gate `7df464a`, sandboxed scripts `0fe99d8`).
- **Extras delivered beyond the original 149-item audit** (now reflected in
  the verdict table): MCPB-011 governed host runtime verification (`a014678`),
  MCPB-012 working-tree candidate capture (`38dd3b4`), M-AL-01 read-only
  introspection tools, and a governed disposable OpenHands runtime action
  execution capability (`3b792a9`).
- The 2026-08-26 code-level audit found that **many "shipped" milestones are
  PARTIAL, not DONE** — commit messages claimed completion but named pieces
  are missing or unwired (e.g. M-EV-01 has the module but is not wired into
  jobs/snapshot; M-GV-01 has the manager but it is not invoked by handlers;
  M-CT-05's `QUARANTINED_PROVENANCE` is defined but unreachable). Treat the
  audit table below as authoritative.
- Local `main` may be stale; run `git fetch --prune origin` before any
  base-branch reasoning.

> **Implementation history.** The dated snapshot above is a point-in-time
> audit, not a live progress log. Authoritative implementation state lives in
> `git log` and the per-milestone decision records (`docs/MCPB-0NN.md` +
> briefs): MCPB-001→012 and M-AL-01 have all shipped (see `docs/README.md` for
> the milestone index). Subsequent configurability/docs work — making the
> Claude launch command and default effort `.env`-configurable, widening the
> tool `effort` params to optional, and pruning stale session-handoff notes —
> landed after this audit and is not re-tracked here.

> **Security constraints (verbatim, non-negotiable).** Never commit/push/merge/
> tag/restart production or merge without explicit user request. No
> `bypassPermissions` or unrestricted Bash; no `shell=True`; fixed argument
> vectors. Reject hidden/protected/traversal/symlink paths; `.env`/tokens/
> certs/`.venv` never in archives or Git. The only push surface is opt-in
> `push_development_commit` (non-trunk, origin-only, no-force). ChatGPT
> confirmation is the outer authorization gate. The bridge never directly edits
> its production checkout (candidate work goes through detached git worktrees).
> Every milestone that needs a **new** tool/capability is flagged
> **NEW SURFACE — needs authorization, dormant by default** and must add a
> `ProjectCapability` (requiring its dependency capability), register a tool
> with `ToolAnnotations`, and ship off by default.

## Current state (2026-08-28) — supersedes the audit rows below

Verified against the source on 2026-08-27/28 (grep/symbol evidence, not just
commit messages). **All R1 Continuity, R2 Evidence, R3 Governance, and the
cross-cutting M-CC set are now DONE, including the three former PARTIAL rows
closed on 2026-08-28.** The 2026-08-27 integration re-landed the full
capability set onto `main` (22 cherry-picked commits, full suite green: **572
tests**), closing every item in the prior "definitive open set". The 2026-08-28
PARTIAL-closure tail then added three more DONE items — full suite green:
**591 tests**:

- **Deterministic replay (NEW SURFACE):** read-only `replay_verification` tool
  + `REPLAY_VERIFICATION` capability (requires `verification_commit`, dormant
  by default) — re-runs recorded commands in the hermetic lab against a
  read-only detached checkout at the recorded `base_head`, recomputes env +
  per-command digests, and surfaces digest/result mismatches honestly
  (`tests/test_replay.py`). Tool surface **38 → 39**.
- **Test-kind coverage:** `VerificationCommand.kind`
  (`unit|integration|property|mutation|ad_hoc`, default `unit`); property
  commands run under a deterministic seed derived from `base_head`+`command.id`;
  mutation output parses best-effort into `mutation_stats`; kind folds into
  digests only when non-unit (`tests/test_verification.py` TestKindTests).
- **Static analysis / schema validation (internal):** `ProjectPolicy.
  static_analysis` block — declared analyzers over the candidate via the
  hermetic runner with structured findings, stdlib-only schema checks, and
  opt-in `fail_on="error"`; persisted in the `verification_static_analysis`
  sidecar (`tests/test_verification.py` StaticAnalysisTests).

Below, the 2026-08-27 integration details (unchanged, still accurate):

- **M-CT-02 … M-CT-07 all DONE** — `CLAUDE_HARD_WALL_SECONDS` +
  compute-proxy (`config.py`), `job_checkpoint_detail` sidecar (`store.py`),
  `completed_milestones`/`remaining_milestones` (`jobs.py`),
  `QUARANTINED_PROVENANCE` reachable (`jobs.py:2288`), `diff_summary` +
  `validation_evidence` (`jobs.py:1512`), `_detect_progress_intelligence`
  (`jobs.py:892`).
- **M-EV-01 … M-EV-08 all DONE** — `requirements.py` + `RequirementManager`
  wired into jobs/snapshots (`requirements_covered` at `jobs.py:1491-1502`),
  `environment_digest`, bindings sidecars, `verdict_hash` +
  `revision_resolves_finding`, `source_classifications` +
  `authority_records`, `export_evidence_bundle`, `nondeterminism_labeled`,
  `gc_artifacts`.
- **M-GV-01 … M-GV-07 all DONE** — `authorization.py:AuthorizationManager` +
  `authorizations` table wired into server handlers; named roles Planner /
  Evidence-officer / Supervisor (read-only command builders +
  `tests/test_roles.py`); `apex_authority_ref` + `REQUIRE_APEX_GROUNDING`
  (`tests/test_registry.py`); `ratified_spec_commit` spec-gating
  (`tests/test_jobs.py`); `drafter_id` self-certification guard
  (`tests/test_review.py`); capability-subset runtime enforcement for
  delegated children (`require_job_capability`, `tests/test_delegation.py`);
  parallel verify + independent review with `reconcile_reviews`
  (`review.py:1024`, `tests/test_review.py:1409+`).
- **M-CC-01 … M-CC-11 all DONE** — `pr.py:218` discover-then-create/update
  open_pull_request + race-safe idempotency concurrency test; `ratify_candidate`
  + `ratifications` sidecar (Option A, merge stays prohibited); `unexpected_paths`
  commit-gate detection; `recover_completed_worktree` read-only re-attach
  (`worktrees.py:672`); the full M-CC-08 control surface (6 new controls, each
  `ProjectCapability`-gated, dormant by default — schema locked at 39 tools);
  hermetic verification lab (`PIN_TOOLCHAIN` / `DEPS_CACHE_DIR` /
  `NETWORK_POLICY`); failure-recovery hardening (`reset_worktree_to_base`,
  `recreate_detached`, `remove`, `tests/test_recovery.py`); security hardening
  tail (filesystem/credential policies, signed manifests, sensitivity
  classification, `security.py` + `tests/test_security.py`).

**No open milestones remain** in R1–R3 or Cross-cutting. **M-IN-01 (P3)
stays a non-goal.** The next session's work is release/versioning and any
follow-on hardening, not new capability gaps.

## What MCPB-010 closed (do not rebuild)

- **Resumable checkpoints** — `store.py:91-123` `job_checkpoints`/`job_events`/
  `job_progress` (additive, `SCHEMA_VERSION` stays 1); `jobs.py:434-488`
  `_record_checkpoint`; crash-with-checkpoints → `paused_interrupted`
  (`jobs.py:297-337` `_reload_jobs`).
- **Soft-budget extensions** — `jobs.py:529-533` `_absorb_result_budget` sets
  `soft_warned`; `jobs.py:1099-1126` progress-gated in-place `--resume` slices.
- **Explicit paused state** — `jobs.py:44-48` `PAUSED_BUDGET_EXHAUSTED`/
  `PAUSED_INTERRUPTED`/`BLOCKED_INPUT_REQUIRED` + terminal
  `FAILED_INFRASTRUCTURE`/`QUARANTINED_PROVENANCE`.
- **Exact job resumption** — `jobs.py:680-751` `resume()` keeps `session_id` +
  cumulative budget, seeds from the last checkpoint.
- **Machine-readable completion contract + structured terminal report** —
  `jobs.py:571-635` `_snapshot` returns `prohibited_actions`, `budget`,
  `checkpoint_count`, `milestones`, `blockers`, `pause_reason`,
  `candidate_fingerprint`, `changed_path_count`, `changed_paths`.
- **Multi-dimensional budgets (partial)** — turns/tokens/tool-failures/
  no-progress/per-slice wall-time + hard cap (`config.py:147-160`).
- **Compaction** — `store.py:505-538` `compact_checkpoints`.
- **Append-only job event ledger** — `store.py:104-111` `job_events`.
- **Reviewer independence** — `review.py:581` `run_independent_review` (fresh,
  read-only, non-resumable session); `claude.py:build_reviewer_command`.
- **Canonical sync + base-drift detection** — `sync.py:213` `SyncManager.sync`
  (MCPB-009); `worktrees.py:230-271` `validate_resume`.

## Verdict summary — all 149 ChatGPT capability items

The original point-in-time audit (2026-08-07), reconciled against merged `main`
as of the 2026-08-28 release integration (22 capability commits + the PARTIAL
closure tail, 591 tests green). Every P0/P1/P2 item is delivered — the three
former PARTIAL items (deterministic replay, test-kind coverage, static analysis)
were closed on 2026-08-28; the ten P3 items remain deferred as the M-IN-01
non-goal. Status legend: **DONE** = delivered;
**PARTIAL** = some exists, a named piece is missing; **MISSING** = nothing delivers it.

| Section | Capability | Status | Evidence |
|---|---|---|---|
| P0 | 1. Resumable checkpoints — persist base SHA, worktree, diff, … | DONE | MCPB-010 · store.job_checkpoints/job_progress (additive) + jobs.resume from last checkpoint |
| P0 | 2. Soft-budget extensions — warn at a soft ceiling and grant a bounded … | DONE | jobs._absorb_result_budget sets soft_warned; progress-gated in-place --resume slices |
| P0 | 3. Multi-dimensional budgets — bound turns, tokens, compute, … | DONE | config claude_hard_turns/tokens/wall + tool-failure/no-progress/per-slice ceilings |
| P0 | 4. Explicit paused state — PAUSED_BUDGET_EXHAUSTED / PAUSED_INTERRUPTED / … | DONE | JobStatus.PAUSED_BUDGET_EXHAUSTED/PAUSED_INTERRUPTED/BLOCKED_INPUT_REQUIRED |
| P0 | 5. Exact job resumption — resume the same agent context and c… | DONE | jobs.resume keeps session_id + cumulative budget, seeds from last checkpoint |
| P0 | 6. Canonical-repository synchronization — fetch, verify, and … | DONE | sync.SyncManager + MCPB-009 prepare_canonical_for_work (classify + ff-only) |
| P0 | 7. Base-drift detection — stop or rebase only through an expl… | DONE | worktrees.validate_resume rejects moved base; stale-base guard in create |
| P0 | 8. Atomic milestone execution — decompose work into independe… | DONE | M-CT-04 · completed_milestones/remaining_milestones + acceptance_command_id |
| P0 | 9. Machine-readable completion contract — record outcome, bou… | DONE | jobs._snapshot returns outcome/status/budget/checkpoint_count/milestones |
| P0 | 10. Structured terminal report — return candidate identity, f… | DONE | jobs._snapshot candidate identity/fingerprint/changed_paths/blockers/validation_evidence |
| P1 | Named role: Planner (converts ratified requirement set into b… | DONE | M-GV-02 · build_planner_command (read-only Read/Glob/Grep); tests/test_roles.py |
| P1 | Named role: Implementer (changes only the authorized candidat… | DONE | jobs._run creates a detached worktree, edits only candidate paths |
| P1 | Named role: Test operator (runs fixed declared verification) | DONE | verification.verify runs each project.verification_commands command |
| P1 | Named role: Independent reviewer (reads but cannot modify the… | DONE | review.run_independent_review spawns a fresh, read-only, non-resumable session |
| P1 | Named role: Evidence officer (checks fingerprints, bindings, … | DONE | M-GV-02 · build_evidence_officer_command + M-EV-03 binding checks |
| P1 | Named role: Publisher (performs authorized Git/GitHub mutatio… | DONE | verification.PublicationManager + M-GV-01 authorization gate |
| P1 | Named role: Supervisor (detects loops, drift, budget pressure… | DONE | M-GV-02 · build_supervisor_command + M-CT-07 progress intelligence |
| P1 | Support: bounded sub-agent delegation | DONE | M-GV-06 · parent_job_id + delegation_mandate_json + MAX_DELEGATIONS_PER_JOB |
| P1 | Support: parent/child task trees | DONE | M-GV-06 parent/child tree recorded; terminal report surfaces it |
| P1 | Support: capability-aware routing | DONE | ProjectCapability gating of every tool surface (models.py:13-20) |
| P1 | Support: parallel read-only verification and review | DONE | M-GV-07 · asyncio.gather verify + independent review once candidate stable |
| P1 | Support: reviewer independence constraints | DONE | review.py structural independence (fresh read-only non-resumable reviewer) |
| P1 | Support: conflict reconciliation between reviewers | DONE | M-GV-07 · reconcile_reviews + tie-breaker with distinct reviewer_id |
| P1 | Support: replacement of a stalled worker without replacing th… | DONE | M-CC-08 · replace_worker control |
| P1 | Support: delegation records showing mandate/inputs/outputs/au… | DONE | M-GV-06 · delegation_mandate_json persisted on the child job |
| P1 | Support: prevention of self-certification (no agent may certi… | DONE | M-GV-05 · record_candidate_review rejects reviewer_id == drafter_id |
| P1 | Content-address every candidate, checkpoint, review, test res… | DONE | _candidate_state digest + review verdict_hash + checkpoint partial_fingerprint |
| P1 | Bind every verification and review to: repository identity; b… | DONE | M-EV-03 · verification_bindings/review_bindings sidecars (toolchain_digest, authority_ref, expires_at) |
| P1 | Maintain an append-only job event ledger | DONE | store.job_events (autoincrement event_id, append-only) |
| P1 | Preserve source classifications: canonical, historical, asser… | DONE | M-EV-05 · source_classifications table + seeded vocabulary |
| P1 | Record the authority behind every governance-changing instruc… | DONE | M-EV-05 · authority_records table; _record_event stamps authority_ref |
| P1 | Link revisions to the review findings they resolve | DONE | M-EV-04 · revision_resolves_finding table + closure chain |
| P1 | Automatically invalidate prior attestations whenever the cand… | DONE | verification.commit recomputes attestations on candidate change |
| P1 | Export a portable evidence bundle suitable for offline audit | DONE | M-EV-06 · export_evidence_bundle (requirement coverage + sensitive-path filter) |
| P1 | Support deterministic replay where feasible | DONE | 2026-08-28 · NEW SURFACE `replay_verification` read-only tool (REPLAY_VERIFICATION cap, requires `verification_commit`, dormant by default): re-runs the recorded commands in the hermetic lab against a read-only detached checkout at the recorded `base_head`, recomputes env + per-command digests, surfaces digest/result mismatches honestly (tests/test_replay.py) |
| P1 | Detect nondeterministic verification and label it honestly | DONE | M-EV-07 · deterministic repetitions → nondeterminism_labeled + DEGRADED |
| P1 | Provide artifact retention and garbage-collection policies wi… | DONE | M-EV-08 · gc_artifacts (promoted-evidence-safe, retention days) |
| P1 | Track: files inspected and changed; requirements covered; tes… | DONE | checkpoints (changed_paths) + requirements_covered + test evidence |
| P1 | Automatically stop or escalate when it detects: repeated iden… | DONE | M-CT-07 oscillating/scope-growth/weakened-tests + no-progress/hard ceilings |
| P1 | Conversely, grant a bounded extension when the candidate is a… | DONE | progress-gated in-place --resume slices (jobs._run) |
| P1 | Parse normative requirement IDs from the canonical specificat… | DONE | M-EV-01 · RequirementManager.parse (normative pattern + fenced block) |
| P1 | Produce a requirement-to-code-to-test traceability matrix | DONE | M-EV-01 · RequirementManager.matrix joins requirements → requirement_links → paths |
| P1 | Detect unimplemented or untested requirements | DONE | M-EV-01 · matrix covered/uncovered sections |
| P1 | Distinguish normative requirements from examples and commenta… | DONE | M-EV-01 · kind classification (normative\|example\|commentary) |
| P1 | Identify contradictory requirements before coding | DONE | M-EV-01 · contradictory flag in matrix |
| P1 | Record intentional deferrals and their authority | DONE | M-EV-01 · deferred status + deferred_authority |
| P1 | Check lifecycle tables and schemas for internal consistency | DONE | M-EV-01 lifecycle-consistency checks |
| P1 | Verify that runtime behavior fails closed where required | DONE | fail-closed validators/gates throughout (fingerprint, capability, mandate) |
| P1 | Generate a coverage report without falsely equating line cove… | DONE | M-EV-01 · requirements_covered matrix distinguishes requirement coverage from line coverage |
| P1 | Step 1 — resolve canonical repo+remote | DONE | registry.py resolves ProjectPolicy.root + remote |
| P1 | Step 2 — fetch exact base | DONE | M-CT-01 · requested_base threads through jobs; fetch-then-pin where opted in |
| P1 | Step 3 — verify clean canonical state | DONE | worktrees.create checks before_state clean |
| P1 | Step 4 — create isolated worktree | DONE | worktrees.create (git worktree add --detach) |
| P1 | Step 5 — enforce path/scope restrictions | DONE | reader path guards + M-CC-06 unexpected_paths policy |
| P1 | Step 6 — inspect diff | DONE | review.list_candidate_changes |
| P1 | Step 7 — run verification | DONE | verification.verify runs declared commands |
| P1 | Step 8 — request independent review | DONE | review.run_independent_review |
| P1 | Step 9 — revise without losing history | DONE | jobs.resume re-runs the same Claude session |
| P1 | Step 10 — re-run invalidated gates | DONE | verify_development_job re-derives + re-verifies after changes |
| P1 | Step 11 — create detached verified commit | DONE | verification.commit stages exactly the verified candidate |
| P1 | Step 12 — publish a new branch | DONE | verification.PublicationManager.publish |
| P1 | Step 13 — open or update a draft PR | DONE | M-CC-01 · open_pull_request (gh pr list/create/edit, draft) |
| P1 | Step 14 — confirm remote branch/PR state | DONE | M-CC-02 · remote_branch_state + pr_state in publication result |
| P1 | Missing — refresh/synchronize canonical checkouts on demand | DONE | sync.SyncManager.sync + prepare_canonical_for_work tool |
| P1 | Missing — branch and PR race-safe idempotency | DONE | M-CC-05 · concurrency test (two open_pull_request → one create + one edit) |
| P1 | Missing — existing-PR discovery before and after creation | DONE | M-CC-01 · gh pr list --head discovery before create |
| P1 | Missing — update an existing draft PR without creating duplic… | DONE | M-CC-01 · discover-then-edit path |
| P1 | Missing — verify remote tip after push | DONE | verification._push_and_verify re-reads remote tip |
| P1 | Missing — detect unexpected files, generated artifacts, or su… | DONE | M-CC-06 · unexpected_paths flag + commit-gate rejection; submodule rejection |
| P1 | Missing — cleanly abandon a candidate without touching canoni… | DONE | M-CC-08 · discard_candidate (worktree remove) + cancel() |
| P1 | Missing — recover completed-job worktrees read-only | DONE | M-CC-07 · worktrees.recover_completed_worktree (detached, fingerprint-verified, read_only) |
| P1 | Missing — create a successor job from an immutable completed … | DONE | M-CC-03 · create_successor_job pins successor_base_head to parent committed_sha |
| P1 | Missing — distinguish merge incorporation from governance rat… | DONE | M-CC-04 · ratifications sidecar distinct from merge (Option A) |
| P1 | Missing — enforce separate authorization for commit, push, PR… | DONE | M-GV-01 · authorizations table + capability gates per surface |
| P2 | Budget exhausted -> pause with resumable checkpoint | DONE | no-progress/hard-turns/wall ceilings pause as PAUSED_BUDGET_EXHAUSTED |
| P2 | Model/provider interruption -> resume from last durable check… | DONE | _reload_jobs reconciles crash → paused_interrupted; resume() from last checkpoint |
| P2 | Tool failure -> retry within policy then isolate as infrastru… | DONE | M-CC-10 · tool-failure retry→isolate as FAILED_INFRASTRUCTURE |
| P2 | Test failure -> preserve candidate and return to implementati… | DONE | verification.verify preserves candidate; revise path re-runs gates |
| P2 | Review rejection -> preserve findings, revise, invalidate old… | DONE | revision_resolves_finding links revisions; invalidated gates re-run |
| P2 | Remote base advanced -> pause for rebase/restart decision | DONE | worktrees.validate_resume raises base-moved; stale-base guard |
| P2 | Candidate corruption -> roll back to last verified checkpoint | DONE | M-CC-10 · reset_worktree_to_base on re-derive failure |
| P2 | Publication race -> reconcile remote state idempotently | DONE | verification.publish handles branch-idempotent reconciliation |
| P2 | Lost worktree -> reconstruct from content-addressed patch and… | DONE | M-CC-10 · recreate_detached at base_head after cat-file check |
| P2 | Compromised provenance -> quarantine, do not promote | DONE | QUARANTINED_PROVENANCE reachable on fingerprint/re-derive corruption |
| P2 | Immutable completed jobs should remain immutable, but BridgeL… | DONE | M-CC-03 · successor job from immutable completed candidate |
| P2 | Model: actor identity; institutional role; authority source; … | DONE | M-GV-01 actor/authority_source on authorizations; M-EV-05 authority_records |
| P2 | Policy: editing allowed, publishing disallowed | DONE | models.py ProjectCapability separates CLAUDE_EDIT from publish caps |
| P2 | Policy: publishing a named branch allowed, merging disallowed | DONE | verification._validate_branch_name; 'merge' in PROHIBITED_ACTIONS |
| P2 | Policy: ratification distinct from merge | DONE | M-CC-04 · ratify_candidate internal act; bridge never merges |
| P2 | Policy: runtime implementation allowed only under a named rat… | DONE | M-GV-04 · ratified_spec_commit pins job spec; drift rejected |
| P2 | Policy: tagging and deployment requiring new authorization | DONE | 'tag'/'deploy' in PROHIBITED_ACTIONS (never-authorized) |
| P2 | Policy: destructive operations requiring explicit target conf… | DONE | ToolAnnotations(destructive) + target re-confirmation on every destructive tool |
| P2 | Policy: external grounding required for genesis/apex authority | DONE | M-GV-03 · apex_authority_ref + require_apex_grounding; registry rejects ungrounded |
| P2 | Cross-cutting: every sensitive tool call checked against this… | DONE | M-GV-01 AuthorizationManager wired into every sensitive server handler |
| P2 | Dashboard: exact canonical base | DONE | _snapshot['base_head'] |
| P2 | Dashboard: current candidate fingerprint | DONE | _snapshot['candidate_fingerprint'] from latest checkpoint |
| P2 | Dashboard: changed paths | DONE | _snapshot['changed_paths'] + changed_path_count |
| P2 | Dashboard: requirements covered | DONE | M-EV-01 · requirements_covered in snapshot |
| P2 | Dashboard: tests and review verdicts | DONE | get_candidate_review + validation_evidence |
| P2 | Dashboard: unresolved concerns | DONE | _snapshot['blockers'] |
| P2 | Dashboard: budget consumed and requested extension | DONE | _snapshot['budget'] |
| P2 | Dashboard: authorities currently in force | DONE | M-CC-08 · authorities_in_force dashboard surface |
| P2 | Dashboard: irreversible next action | DONE | PROHIBITED_ACTIONS enumerated per status |
| P2 | Controls: pause | DONE | M-CC-08 · pause_development_job control (idempotent) |
| P2 | Controls: resume | DONE | resume_development_job (server.py) |
| P2 | Controls: extend budget | DONE | M-CC-08 · extend_budget control (bounded, recorded) |
| P2 | Controls: narrow scope | DONE | M-CC-08 · narrow_scope control (scope_paths_json) |
| P2 | Controls: replace worker | DONE | M-CC-08 · replace_worker control |
| P2 | Controls: discard candidate | DONE | M-CC-08 · discard_candidate control (worktrees.remove) |
| P2 | Controls: authorize a specific mutation | DONE | commit_verified_development_job (scoped, target-specific) |
| P2 | Controls: revoke unused authorization | DONE | M-CC-08 · revoke_authorization control |
| P2 | Controls: request another reviewer | DONE | run_independent_review tool |
| P2 | Controls: export evidence | DONE | M-EV-06 · export_evidence_bundle |
| P2 | Approvals structured and target-specific; Proceed must not ac… | DONE | commit_verified_development_job restates evidence; target-specific |
| P2 | Per-job filesystem, network, credential, and tool policies | DONE | M-CC-09/11 · filesystem_policy/network_policy/credential_policy on ProjectPolicy |
| P2 | Least-privilege short-lived credentials | DONE | security.py projected_credential_env / credential_names (only declared env projected) |
| P2 | Secret redaction from logs, patches, and model context | DONE | path/env redaction filters throughout reader/evidence/diff |
| P2 | Dependency and supply-chain scanning | DONE | M-CC-11 · supply_chain_scans run pre-commit, severity-gated |
| P2 | Protection against repository prompt injection | DONE | reader_injection_defense fences untrusted content |
| P2 | Allowlisted publication destinations | DONE | PublicationManager allowlisted branches/remotes |
| P2 | Signed job manifests and evidence artifacts | DONE | M-CC-11 · sign_evidence_bundle (HMAC-SHA256, key outside repo) |
| P2 | Audit of all external writes | DONE | durable append-only audit tables (authorizations, captures, events) |
| P2 | No silent access expansion during extensions | DONE | extension logic never widens capabilities |
| P2 | Sensitivity classification before evidence export | DONE | M-CC-11 · sensitivity_policy gates evidence-bundle inclusion |
| P2 | Sandboxed execution of repository-controlled scripts | DONE | bwrap sandbox for repo-controlled verification scripts |
| P2 | Hermetic or strongly controlled verification environments | DONE | M-CC-09 · hermetic-lab _run_command |
| P2 | Pinned dependency and toolchain versions | DONE | M-CC-09 · toolchain_pin / PIN_TOOLCHAIN replaces {python} |
| P2 | Cached dependencies without contaminating results | DONE | M-CC-09 · DEPS_CACHE_DIR bound read-only |
| P2 | Network policy per validation | DONE | M-CC-09 · NETWORK_POLICY none\|allowlist enforced in sandbox |
| P2 | Deterministic clock, locale, timezone, randomness, filesystem… | DONE | TZ=UTC / SOURCE_DATE_EPOCH / PYTHONHASHSEED / LANG=C.UTF-8 |
| P2 | Test-kind coverage: unit, integration, property, mutation, ad… | DONE | 2026-08-28 · `VerificationCommand.kind` (`unit|integration|property|mutation|ad_hoc`, default `unit` keeps existing projects unchanged); property commands run under a deterministic seed derived from `base_head`+`command.id` (repetition-suffixed); mutation output parses best-effort into `mutation_stats`; kind folds into command/env digests only when non-unit (tests/test_verification.py TestKindTests) |
| P2 | Static analysis and schema validation | DONE | 2026-08-28 · internal `ProjectPolicy.static_analysis` block (no new tool/capability): declared analyzers run read-only via the same hermetic runner, output parsed into structured findings; stdlib-only `schema_checks` (json/yaml-as-TOML/xml) over changed files; `fail_on="error"` (opt-in) flips the conclusion to FAIL, default `"never"` stays advisory; results persist in the `verification_static_analysis` sidecar (tests/test_verification.py StaticAnalysisTests) |
| P2 | Performance and resource ceilings | DONE | wall-clock timeout in _run_exec + config ceilings (turns/tokens/wall/tool-failures) |
| P2 | Flaky-test detection (deterministic repetitions) | DONE | M-EV-07 · verification_deterministic_repetitions labels divergence |
| P2 | Baseline comparison | DONE | M-CC-09 · baseline_fingerprint + baseline_matches vs last green verification |
| P2 | Immutable logs | DONE | store.job_events append-only (autoincrement event_id) |
| P2 | Signed or otherwise integrity-bound results | DONE | content-addressed candidate/verdict + HMAC-signed evidence bundle |
| P2 | Clear separation between infrastructure failure and candidate… | DONE | infrastructure_status + FAILED_INFRASTRUCTURE vs FAILED_CANDIDATE |
| P2 | Structured verification result schema (candidate_fingerprint,… | DONE | M-EV-02 · verify() returns candidate_fingerprint/base_commit/environment_digest/commands/conclusion |
| P3 | Capability Capsules from successful missions | MISSING | deferred — M-IN-01 (P3) non-goal |
| P3 | Capability Passports for agents and reviewers | MISSING | deferred — M-IN-01 (P3) non-goal |
| P3 | Recorded failure modes and required interventions | MISSING | deferred — M-IN-01 (P3) non-goal |
| P3 | Reconstruction dossiers for implemented modules | MISSING | deferred — M-IN-01 (P3) non-goal |
| P3 | Blind-reconstruction experiments | MISSING | deferred — M-IN-01 (P3) non-goal |
| P3 | Portability checks across runtimes and models | MISSING | deferred — M-IN-01 (P3) non-goal |
| P3 | Capability compatibility constraints | MISSING | deferred — M-IN-01 (P3) non-goal |
| P3 | Evidence-based agent selection | MISSING | deferred — M-IN-01 (P3) non-goal |
| P3 | Delta qualification when a capability contract changes | MISSING | deferred — M-IN-01 (P3) non-goal |
| P3 | Versioned lessons that improve future workflows without smugg… | MISSING | deferred — M-IN-01 (P3) non-goal |
## 2026-08-26 true status audit (authoritative)

A code-level re-audit of every milestone against the actual source (25
verified by parallel read-only subagents, 13 verified by direct grep/Read
after the subagent fleet hit the ollama session-usage cap). **This table
supersedes the dated verdict summary above.** Status counts: **8 DONE,
22 PARTIAL, 8 MISSING** (of 38 milestones audited, including 4 extras not in
the original 149-item table). M-IN-01 remains an explicit non-goal.

The "Gap" column is the concrete remaining work to reach DONE; it is the
authoritative input to the implementation order in "Recommended next action".

| Milestone | Title | Status | Gap (remaining work to DONE) |
|---|---|---|---|
| M-CT-01 | Worktree base fetch + pin | DONE | — (worktrees.py:267 create(requested_base); _resolve_remote_default_tip fetches origin; jobs.py:1508 threads requested_base; tests/test_mct.py) |
| M-CT-02 | Compute + cumulative wall-time budgets | PARTIAL | Add `CLAUDE_HARD_WALL_SECONDS` (cumulative wall) + a `wall_seconds_used` job field persisted in job_progress; `_absorb_result_budget` accumulates per-slice wall; pause on cumulative ceiling; tests. |
| M-CT-03 | Checkpoint rich fields | MISSING | Create sidecar `job_checkpoint_detail` (completed_steps_json/remaining_steps_json/tests_json/decisions_json/tool_history_json); wire `_record_checkpoint` to parse `CLAUDE_STREAM_JSON` events; rehydration; tests. |
| M-CT-04 | Atomic milestone execution | PARTIAL | Add `completed_milestones`/`remaining_milestones` to `_snapshot` (partition by `closed`); test the `acceptance_command_id` pass/fail/unknown branches; (optional) typed `Milestone` model. |
| M-CT-05 | BLOCKED_INPUT_REQUIRED + QUARANTINED_PROVENANCE reachable | PARTIAL | Make `_run` enter `QUARANTINED_PROVENANCE` on real provenance corruption (fingerprint mismatch / re-derive failure) instead of raising PublicationError; add a stream-json `input required` signal to `_blocked_input_signal`; reachability tests. |
| M-CT-06 | Diff summary + validation evidence in terminal report | PARTIAL | Add `diff_summary` (bounded redacted `git diff --stat`) and `validation_evidence` (pass/fail + ids when a verifications/reviews row exists) to `jobs.py:_snapshot`; tests. |
| M-CT-07 | Progress-intelligence enrichment | DONE | Compare consecutive checkpoints in `_record_checkpoint` for oscillating edits / scope growth / weakened tests; pause with distinct `pause_reason` per trigger; tests. |
| M-EV-01 | Requirement traceability | PARTIAL | Wire `RequirementManager` into jobs/server (scan/parse on job create, persist links); surface `requirements_covered` in `get_development_job`/`get_project_status`; full matrix() covered/uncovered/contradictory + deferrals-with-authority; tests. |
| M-EV-02 | Structured verification result schema + env fingerprint | DONE | — (verification.py:compute_environment_digest + _command_digest; VerificationRecord.environment_digest; verify() structured response fields candidate_fingerprint/base_commit/environment_digest/conclusion + per-command id/command_digest; store.verification_environment sidecar; tests/test_verification.py M-EV-02 cases) |
| M-EV-03 | Evidence binding completeness | DONE | — (store.verification_bindings+review_bindings sidecars with toolchain_digest/reviewer_independence_class/expires_at/authority_ref; verification.py VerificationRecord.expires_at/authority_ref + _is_expired commit-gate check; review.py ReviewRecord.reviewer_independence_class + ReviewManager stamps bridge_spawned/human/external + validate_for_commit expiry check; tests/test_verification.py + tests/test_review.py M-EV-03 cases) |
| M-EV-04 | Review content-addressing + revision→finding links | DONE | — (store.review_content sidecar with verdict_hash = SHA-256 over verdict + base_head + diff_fingerprint + sorted paths; store.revision_resolves_finding table; review.py compute_verdict_hash + ReviewRecord.verdict_hash + _persist_review/_reload_reviews wire the sidecar; jobs.py record_revision_resolves_findings records non-fatal revision→finding links; ReviewManager.get_review_closure_chain queryable chain; tests/test_review.py M-EV-04 cases) |
| M-EV-05 | Source classifications + authority-behind-instructions | DONE | — (source_classifications + authority_records tables, seeded vocabulary, _record_event stamps authority_ref; tests) |
| M-EV-06 | Portable evidence bundle export | DONE | — (NEW SURFACE shipped earlier: `ProjectCapability.EXPORT_EVIDENCE_BUNDLE` + `export_evidence_bundle` tool; this commit closes the audit gap: `RequirementManager` coverage section wired into `EvidenceBundleManager.export_bundle` via lazy `jobs._requirements_manager()`; explicit `.env`/`.venv`/token/cert/`.pem`/`.key`/secrets/`.aws`/`.ssh` exclude filter on changed_paths + diff detail with `sensitive_excluded_count`; non-fatal requirement section; tests/test_evidence_bundle.py SensitivePathFilterTests + coverage cases; zip serialization deferred as optional) |
| M-EV-07 | Nondeterminism detection + deterministic replay | DONE | — (verification.py:verify runs each command `verification_deterministic_repetitions` times and labels divergence via `_commands_diverge` comparing status/exit_code/stderr → `nondeterminism_labeled` + `infrastructure_status=DEGRADED`; `compute_environment_digest` + per-command `command_digest` (M-EV-02) persisted; tests/test_verification.py M-EV-07 flaky + deterministic-control cases; `replay_verification` NEW-SURFACE tool intentionally deferred — no new surface shipped here) |
| M-EV-08 | Artifact retention + GC policy | DONE | — (`config.py` `evidence_retention_days`/`checkpoint_retention_days` (env `EVIDENCE_RETENTION_DAYS`/`CHECKPOINT_RETENTION_DAYS`, default 0 = disabled); `store.gc_artifacts` deletes non-promoted non-structural checkpoint+detail rows older than retention, never verifications/reviews/publications/syncs; promoted-evidence guard via protected job_id/checkpoint_id sets; testable via `now_iso` cutoff param; additive `idx_job_checkpoints_recorded_at` index; tests/test_checkpoints.py `ArtifactGcTests`) |
| M-GV-01 | Durable authorization objects | DONE | |
| M-GV-02 | Named roles Planner / Evidence-officer / Supervisor | DONE | — (`claude.py` role builders `build_planner_command`/`build_evidence_officer_command`/`build_supervisor_command`, read-only Read/Glob/Grep only; capability-gated invocations; tests/test_roles.py) |
| M-GV-03 | External grounding for genesis/apex authority | DONE | — (`ProjectPolicy.apex_authority_ref` + `require_apex_grounding`; `registered_apex_authority` table; registry rejects ungrounded policy; genesis jobs require ref present+unchanged; tests/test_registry.py) |
| M-GV-04 | Spec-gated runtime under ratified spec commit | DONE | — (`ProjectPolicy.ratified_spec_commit`; `create_development_job` requires job spec pinned to that commit, rejects spec drift; tests/test_jobs.py) |
| M-GV-05 | Self-certification prevention | DONE | — (`drafter_id` recorded on the job; `record_candidate_review` rejects reviewer_id == drafter; self-review test; tests/test_review.py) |
| M-GV-06 | Sub-agent delegation + parent/child task trees | DONE | — (`parent_job_id` + `delegation_mandate_json`; `MAX_DELEGATIONS_PER_JOB`; fail-closed mandate validation; `require_job_capability` enforces child's invoked caps ⊆ mandate authority at runtime; tests/test_delegation.py) |
| M-GV-07 | Parallel verify + review + conflict reconciliation | DONE | — (`asyncio.gather` verify + independent review once the candidate is stable; `reconcile_reviews` wired into the commit gate (reconciled pass over multiple review_ids); tie-breaker review with distinct reviewer_id; tests/test_review.py) |
| M-CC-01 | PR creation + existing-PR discovery + update draft PR | DONE | — (pr.py PullRequestManager; open_pull_request tool server.py:1202; OPEN_PULL_REQUEST cap; gh pr list/create/edit; tests/test_pr.py) |
| M-CC-02 | Confirm remote branch/PR state | DONE | — (pr.py:364-365 remote_branch_state+pr_state; introspection.py:717) |
| M-CC-03 | Successor job from immutable completed candidate | DONE | — (`create_successor_job(parent_job_id, instructions)` pins `successor_base_head` to the parent's `committed_sha` (not canonical HEAD), requires parent COMPLETED + committed_sha + worktree; parent preserved immutable; tests/test_checkpoints.py + test_jobs.py) |
| M-CC-04 | Merge-vs-ratification + separate authz | DONE | — (`ratifications` sidecar table + internal `ratify_candidate` act, distinct from merge (Option A); merge/tag/release/deploy stay prohibited; tests) |
| M-CC-05 | Branch/PR race-safe idempotency | DONE | — (concurrency test: two `open_pull_request` via asyncio.gather under shared manager + mocked `_gh` → exactly one create + one edit; tests/test_pr.py) |
| M-CC-06 | Unexpected-files / generated-artifact / submodule detection | DONE | — (configurable `unexpected_paths` policy; flag + reject at commit gate; tests/test_verification.py) |
| M-CC-07 | Read-only completed-worktree recovery | DONE | — (`worktrees.recover_completed_worktree` re-attaches the vanished completed worktree as a detached checkout at `committed_sha`, re-derives + verifies the candidate fingerprint via `committed_state`, returns `JobWorktree(read_only=True)`; tests) |
| M-CC-08 | Human control surface | DONE | — (`authorities_in_force` dashboard surface; 6 control tools `pause_development_job`/`extend_budget`/`narrow_scope`/`replace_worker`/`discard_candidate`/`revoke_authorization`, each ProjectCapability-gated, dormant by default; schema locked at 39 tools after the 2026-08-28 `replay_verification` addition; tests/test_controls.py + test_server.py/test_checkpoints.py/test_persistence.py canaries) |
| M-CC-09 | Verification-lab determinism | DONE | — (hermetic `_run_command`: `PIN_TOOLCHAIN` pinned venv, `DEPS_CACHE_DIR` read-only deps cache, `NETWORK_POLICY` none\|allowlist; TZ=UTC/SOURCE_DATE_EPOCH/PYTHONHASHSEED; `verification_deterministic_repetitions` flaky labeling + baseline; tests/test_verification.py) |
| M-CC-10 | Failure-recovery hardening | DONE | — (`reset_worktree_to_base` corruption rollback, `recreate_detached` lost-worktree reconstruction at `base_head`, `remove` discard; tool-failure retry→isolate as FAILED_INFRASTRUCTURE; quarantine machinery; `job_worktree_meta` table; tests/test_recovery.py) |
| M-CC-11 | Security hardening | DONE | — (`security.py` filesystem/credential policies, signed manifests, sensitivity classification; bwrap sandbox; supply_chain_scans pre-commit; reader_injection_defense; tests/test_security.py) |
| M-IN-01 | P3 capability learning & reconstruction | MISSING | Explicitly deferred (non-goal) until R1–R3 prove out. No P3 tables exist. |
| **X-MCPB-011** | Governed host runtime verification (extra) | DONE | — (RUNTIME_VERIFICATION cap models.py:34; runtime_verification_commands; docs/MCPB-011.md) |
| **X-MCPB-012** | Working-tree candidate capture (extra) | DONE | — (WORKING_TREE_CANDIDATE_CAPTURE cap models.py:43; docs/MCPB-012.md) |
| **X-M-AL-01** | Read-only introspection tools (extra) | DONE | — (list_development_jobs/get_effective_project_policy/resolve_candidate_canonicalization tools server.py:1269/1307/1343; job_introspection/policy_introspection/canonical_history_read caps; docs/M-AL-01.md) |
| **X-OPENHANDS** | Governed disposable runtime action execution (extra) | DONE | — (open_hands_action.py OpenHandsActionManager; GOVERNED_DISPOSABLE_RUNTIME_ACTION_EXECUTION cap models.py:44; execute_action tool server.py:1604; test_open_hands_action.py) |

**Implementation order (unblock value first):** M-CT-04 → M-CT-05 → M-EV-01
(the three CKS-002B blockers still open), then the small additive R1 tail
(M-CT-02/03/06/07), then R2 Evidence (M-EV-02→08), then R3 Governance
(M-GV-02→07, building on M-GV-01 wiring), then Cross-cutting (M-CC-03→11).
M-IN-01 stays deferred.

## Closure plan — by release

The unclosed items (PARTIAL + MISSING) are grouped into the four releases
ChatGPT recommended plus a Cross-cutting section, as implementable milestones.
DONE items appear only in the verdict summary and the "closed" section above.
Each milestone lists **Status**, **Evidence**, **Gap**, and **Build**
(files / approach / schema / acceptance). Items needing a new MCP tool or
capability are flagged **NEW SURFACE**.

### Release 1 — Continuity (remaining items; most is closed by MCPB-010)

#### M-CT-01 Worktree base fetch + pin (lifecycle step 2)
- **Status:** PARTIAL. **Evidence:** `worktrees.py:128-144` `_source_state`
  derives `base_head` from local `git rev-parse HEAD`; no `git fetch` before
  `create`. **Gap:** a worktree branches off the local canonical HEAD, not a
  fetched/pinned remote commit, so it can branch off a stale base.
- **Build:** `worktrees.py:create` — after the clean check, optionally `git
  fetch origin` (reuse `sync.py` fetch), resolve `origin/<default>` tip, and
  accept/require an optional `requested_base` (40-hex); record it as
  `base_head`. Keep the no-fetch path as the default for offline/local repos
  (fetch is opt-in per project). `jobs.py` threads `requested_base` from the
  job spec. **Tests:** `tests/test_worktrees.py` — fetch-then-pin path uses a
  local bare `origin` (mirror `tests/test_sync.py`); base_head equals the
  remote tip; offline path unchanged. **Acceptance:** a job whose project
  opts in branches off the fetched remote tip, not local HEAD; existing tests
  stay green; no new tool.

#### M-CT-02 Compute + cumulative wall-time budgets
- **Status:** PARTIAL. **Evidence:** `config.py:147-160` has turns/tokens/
  tool-failures/no-progress + per-slice `CLAUDE_TIMEOUT_SECONDS`; no compute,
  no cumulative wall-time. **Gap:** ChatGPT wants turns, tokens, *compute*,
  *wall time* bounded separately and cumulatively.
- **Build:** `config.py` — `CLAUDE_HARD_WALL_SECONDS` (cumulative) and
  `CLUDE_MAX_RESOURCE_TOKENS` (optional cumulative token ceiling already partly
  present as `tokens_used`). Compute budget: capture `usage` cache + (where the
  model exposes it) compute units; if unavailable, document honestly as
  token-proxy. `jobs.py:_absorb_result_budget` accumulates wall-time per slice
  (record slice start via an `args`-passed monotonic stamp — `Date.now` is
  unavailable in workflows only; runtime jobs can use `time.monotonic`). Pause
  as `PAUSED_BUDGET_EXHAUSTED` on cumulative-wall or compute ceiling. **Tests:**
  `tests/test_checkpoints.py` — a fake executor returning large `usage` trips
  the cumulative ceiling; cumulative wall-time cap pauses. **Acceptance:** a
  long multi-slice run pauses on cumulative wall-time, not only per-slice.

#### M-CT-03 Checkpoint rich fields (completed/remaining steps, tests, decisions, tool history)
- **Status:** PARTIAL. **Evidence:** `store.py:91-103` `job_checkpoints` stores
  `changed_paths_json`, `partial_fingerprint`, `tokens_json`, `event_json`,
  `milestone` — no structured completed-steps/remaining-steps/tests/decisions/
  tool-history columns. **Gap:** ChatGPT wants those persisted for replay.
- **Build:** Additive — do NOT alter `job_checkpoints`; add a sidecar
  `job_checkpoint_detail` table keyed by `(checkpoint_id)` with
  `completed_steps_json`, `remaining_steps_json`, `tests_json`,
  `decisions_json`, `tool_history_json` (all NULLABLE). `jobs.py:_record_
  checkpoint` populates what the executor surfaces (stream-json events when
  `CLAUDE_STREAM_JSON`; else best-effort from `event_json`). **Tests:**
  `tests/test_checkpoints.py` — a streaming executor yields detail rows; reload
  re-hydrates them. **Acceptance:** detail rows round-trip; `SCHEMA_VERSION`
  stays 1; no new tool.

#### M-CT-04 Atomic milestone execution (decomposition + acceptance tests)
- **Status:** PARTIAL. **Evidence:** `jobs.py:153` `milestones: list[dict]` is
  tracked + surfaced but never decomposed into independently completable slices
  with acceptance tests. **Gap:** milestones are labels, not execution units.
- **Build:** `models.py`/`jobs.py` — a milestone dict gains `acceptance_paths`
  (ordered) and optional `acceptance_command_id` (a verification command id).
  `_record_checkpoint(kind="milestone")` closes a milestone when its
  `acceptance_paths` appear in the changed set AND its acceptance command
  passes (run via `verification.py:_run_command` read-only). **Tests:**
  `tests/test_checkpoints.py` — a milestone with acceptance paths closes only
  when paths + command pass; an unclosed milestone surfaces in the report as
  remaining. **Acceptance:** terminal report shows `completed_milestones` /
  `remaining_milestones`. Parallel slice execution is explicitly deferred.

#### M-CT-05 Enter BLOCKED_INPUT_REQUIRED + QUARANTINED_PROVENANCE
- **Status:** PARTIAL. **Evidence:** `jobs.py:44-48` defines both but `_run`
  never transitions into them (dormant). **Gap:** the states exist but are
  unreachable.
- **Build:** `jobs.py:_run` — set `BLOCKED_INPUT_REQUIRED` when the drafter
  emits a structured "input required" signal (stream-json event or a fenced
  `<!-- bridge: blocked-input ... -->` block in the result). Set
  `QUARANTINED_PROVENANCE` when verification detects provenance corruption (a
  fingerprint mismatch on an already-verified candidate, or a
  `committed_state` that no longer re-derives) — added in `M-CC-10`. Both are
  terminal; only `BLOCKED_INPUT_REQUIRED` is resumable. **Tests:**
  `tests/test_checkpoints.py` — a blocked-input executor result →
  `blocked_input_required` + `resume_available=True`; a corrupted provenance
  detection → `quarantined_provenance` + not resumable. **Acceptance:** both
  states are reachable and correctly classified for resume.

#### M-CT-06 Diff summary + validation evidence in the terminal report
- **Status:** PARTIAL. **Evidence:** `jobs.py:571-635` `_snapshot` has
  `candidate_fingerprint` + `changed_path_count`/`changed_paths` but no diff
  summary and no validation evidence. **Gap:** ChatGPT wants a diff summary and
  validation evidence in the report.
- **Build:** `jobs.py:_snapshot` — add `diff_summary` (insertions/deletions/
  files changed, computed from a bounded `git diff --stat` over the changed
  paths via `run_git`, redacted) and `validation_evidence` (present only if a
  `verifications` or `reviews` row exists for the job, summarizing
  pass/fail + verification_id/review_id). Keep it additive and bounded.
  **Tests:** `tests/test_jobs.py` — a job with a verification record surfaces
  `validation_evidence`; `diff_summary` matches a known change. **Acceptance:**
  `get_development_job` on a verified job returns both; no new tool.

#### M-CT-07 Progress-intelligence enrichment
- **Status:** DONE. **Evidence:** `jobs.py:_record_checkpoint` now calls
  `_detect_progress_intelligence`, which loads the last
  `claude_progress_intel_window` (default 8) checkpoints and runs three
  additive triggers — oscillating edits, scope growth, weakened tests — each
  pausing as `PAUSED_BUDGET_EXHAUSTED` with a distinct `pause_reason`
  (`oscillating_edits` / `scope_growth` / `weakened_tests`). The triggers
  compose with the existing no-progress/hard-turns/wall (M-CT-02) checks and
  take precedence so the specific reason is not masked. Thresholds are in
  `config.py` (oscillation window 4, alternations 2, scope-growth factor 0.0
  = disabled by default, progress-intel window 8); no new MCP tool, no new
  table, `SCHEMA_VERSION` stays 1, tool-count canary stays 32.
- **Build:** `jobs.py:_record_checkpoint` — compare consecutive checkpoints:
  oscillating edits (a path set flip-flops between two fingerprints over N
  samples, scoped to the most recent changed-path set), scope growth
  (cumulative changed-path count exceeds a declared `delegation_mandate.outputs`
  scope, or `earliest_count * factor` when no declared scope and factor > 0),
  weakened-tests (a test file's assertion count drops while it stays green —
  best-effort via a bounded path-safe worktree read; non-fatal if unreadable).
  On detection, pause as `PAUSED_BUDGET_EXHAUSTED` with a specific
  `pause_reason`; a `progress_intel` audit event is recorded. **Tests:**
  `tests/test_checkpoints.py` — an oscillating executor pauses with the
  oscillation reason; scope-growth pauses; weakened-tests pauses. **Acceptance:**
  the three triggers pause with distinct reasons; no new tool.

### Release 2 — Evidence (the next release to implement)

#### M-EV-01 Requirement traceability (CKS-002B unblock — highest priority)
- **Status:** MISSING. **Evidence:** no `requirements.py`, no `requirements`
  table (`grep` finds nothing); `reader.py:261` `RepositoryReader.outline`
  returns headings only. **Gap:** all nine traceability bullets — parse
  normative IDs, build a requirement→code→test matrix, detect
  unimplemented/untested, distinguish normative vs examples/commentary, find
  contradictions, record deferrals, check lifecycle consistency, fail-closed
  check, requirement coverage report.
- **Build:** new `src/local_dev_mcp/requirements.py` with a
  `RequirementManager(settings, store, reader)`. Additive table:
  ```sql
  CREATE TABLE IF NOT EXISTS requirements (
      requirement_id TEXT PRIMARY KEY,        -- normative ID (e.g. CKS-002B-R1)
      project_id TEXT NOT NULL,
      source_path TEXT NOT NULL,
      source_line INTEGER NOT NULL,
      kind TEXT NOT NULL,                      -- normative|example|commentary
      statement TEXT NOT NULL,
      status TEXT,                             -- implemented|unimplemented|deferred
      deferred_authority TEXT,
      updated_at TEXT NOT NULL
  );
  CREATE TABLE IF NOT EXISTS requirement_links (
      link_id INTEGER PRIMARY KEY AUTOINCREMENT,
      requirement_id TEXT NOT NULL,
      job_id TEXT,
      artifact_kind TEXT NOT NULL,             -- code|test|spec
      artifact_path TEXT NOT NULL,
      evidence_json TEXT NOT NULL,
      linked_at TEXT NOT NULL
  );
  ```
  `RequirementManager.parse(project)` extracts normative IDs (a configured
  pattern, default `^[A-Z]+-\d+[A-Z]?-R\d+` plus a fenced normative block) and
  classifies normative vs example/commentary. `matrix(project)` joins
  `requirements` → `requirement_links` → changed paths / test names to produce
  covered/uncovered/contradictory. Deferrals recorded with authority. **Internal
  first** — expose via the existing `get_development_job`/`get_project_status`
  snapshots (add `requirements_covered`), no new tool. A coverage-report tool is
  **NEW SURFACE** (see M-EV-09).
- **Tests:** `tests/test_requirements.py` — parse a fixture spec with
  normative + example blocks; matrix marks an unimplemented requirement; a
  deferral is recorded with authority; contradictory IDs flagged; reload
  re-hydrates; `SCHEMA_VERSION` stays 1.
- **Acceptance:** `get_project_status` for `cairn-knowledge-store` lists
  requirements + coverage; an unimplemented normative requirement is visible
  before any edit. Follows the additive + lazy-import + zero-surface pattern.

#### M-EV-02 Structured verification result schema + environment/toolchain fingerprint
- **Status:** MISSING. **Evidence:** `verification.py:988-997` `verify()`
  returns a flat `{status, verification_id, command_results}`; no
  `environment_digest`, `deterministic_repetitions`, `infrastructure_status`,
  `conclusion`. `VerificationRecord` has no environment digest.
- **Build:** `verification.py` — compute `environment_digest` = SHA-256 over
  the python interpreter version, the allowlisted env keys, the verification
  command argv, and (optionally) a toolchain pin file. Extend the `verify()`
  response to the structured shape:
  ```yaml
  verification:
    candidate_fingerprint: sha256:...
    base_commit: <base_head>
    environment_digest: sha256:...
    commands:
      - {id, command_digest, result}
    deterministic_repetitions: 1   # bumped by M-CC-09 flaky detection
    infrastructure_status: HEALTHY|DEGRADED
    conclusion: PASS|FAIL
  ```
  Keep the existing fields for back-compat (additive). Store
  `environment_digest` on `VerificationRecord` via a sidecar
  `verification_environment` table (do not `ALTER TABLE verifications`).
- **Tests:** `tests/test_verification.py` — the structured fields are present
  and `environment_digest` is stable across identical env; a command digest
  changes when argv changes. **Acceptance:** `verify_development_job` returns
  the structured object; `SCHEMA_VERSION` stays 1; no new tool.

#### M-EV-03 Evidence binding completeness
- **Status:** DONE. **Evidence:** `store.py` `verification_bindings` +
  `review_bindings` sidecar tables (FK with `ON DELETE CASCADE`, `CREATE TABLE
  IF NOT EXISTS`, `SCHEMA_VERSION` stays 1) with `toolchain_digest`,
  `reviewer_independence_class` (`bridge_spawned|human|external`),
  `expires_at`, `authority_ref`. `verification.py:VerificationRecord` carries
  `expires_at`/`authority_ref` and `_is_expired` drives the commit-gate expiry
  check (NULL/absent → permissive). `review.py:ReviewRecord` carries the full
  binding envelope; `run_independent_review` stamps `bridge_spawned`,
  `record_candidate_review` stamps `human` (default) or `external`
  (`external=True`). The existing `bridge-reviewer` `reviewer_id` sentinel is
  preserved for back-compat.
- **Tests:** `tests/test_verification.py`, `tests/test_review.py` — bindings
  round-trip (persist + reload); independence class stamped correctly for
  bridge_spawned vs human vs external; an expired binding is rejected at the
  commit gate; a non-expired/NULL-expiry binding is allowed; malformed expiry
  fails closed. **Acceptance:** every verification + review carries the full
  binding set; no new tool; `SCHEMA_VERSION` stays 1.

#### M-EV-04 Review content-addressing + revision→finding links
- **Status:** MISSING. **Evidence:** `review.py:92-103` `ReviewRecord` has no
  content hash of the verdict; `review.py:730` `validate_for_commit` checks one
  review but links no revision to the findings it resolves.
- **Build:** `review.py` — add `verdict_hash` (SHA-256 over the verdict text +
  binding fields) to a sidecar `review_content` table. When a follow-up job
  revises a candidate, record a `revision_resolves_finding` row linking the new
  job's checkpoint to the resolved review finding ids.
- **Tests:** `tests/test_review.py` — verdict hash is stable; a revision job
  links to the findings it addresses. **Acceptance:** reviews are
  content-addressed; the chain finding→revision→closure is queryable; no new
  tool.

#### M-EV-05 Source classifications + authority-behind-instructions
- **Status:** MISSING. **Evidence:** no classification or authority record
  anywhere (`grep` empty). **Gap:** ChatGPT wants
  canonical/historical/asserted/observed/tested/inferred/superseded labels and
  the authority behind every governance-changing instruction.
- **Build:** sidecar `source_classifications` (artifact ref → classification)
  and `authority_records` (instruction → actor, authority source, scope,
  timestamp). `jobs.py:_record_event` stamps governance-changing events
  (status/extension/pause/terminal) with an `authority_ref`. Reuse the
  authorization objects from M-GV-01 once built; until then record the
  ChatGPT-confirmed authority inline.
- **Tests:** `tests/test_checkpoints.py` — events carry an authority ref;
  classifications round-trip. **Acceptance:** every governance event is
  attributable; no new tool.

#### M-EV-06 Portable evidence bundle export — **NEW SURFACE**
- **Status:** DONE. **Evidence:** `models.py:EXPORT_EVIDENCE_BUNDLE` capability
  + `server.py:export_evidence_bundle` tool shipped earlier (dormant by
  default, tool-count canary at 32). This commit closes the 2026-08-26 audit
  gap: requirement coverage section + sensitive-path exclude filter are wired
  into `evidence.py:export_bundle`.
- **Build:** **NEW** `ProjectCapability.EXPORT_EVIDENCE_BUNDLE` (requires
  `repository_read`); **NEW** tool `export_evidence_bundle(project_id, job_id,
  expected_candidate_fingerprint)` that assembles, into a single redacted
  in-memory JSON/zip, the candidate fingerprint, changed paths + diff summary,
  verification record + bindings, review records + verdict hashes, checkpoint
  summary, and requirement coverage — and returns it (or writes to a path
  inside an allowlisted export root). Sensitive paths redacted; `.env`/tokens/
  certs/`.venv` excluded; **sensitivity classification (M-CC-11) gates
  inclusion**. Dormant by default. Requirement coverage is wired via the lazy
  `jobs._requirements_manager()` helper (no new constructor coupling) and is
  non-fatal (`{"requirements_covered": None}` when unavailable or empty). The
  sensitive-path exclude filter (`_SENSITIVE_PATH_PATTERNS`) drops matching
  `changed_paths` and scrubs diff-stat detail lines, reporting
  `sensitive_excluded_count`. (Optional) zip serialization is deferred — the
  audit gap marked it optional and the JSON bundle is the contract.
- **Tests:** `tests/test_evidence_bundle.py` — bundle contents match a
  verified job; secrets/paths redacted; a project without the capability cannot
  call it; tool-count assertion bumps 22 → 23; requirement coverage section is
  `None` without parsed requirements and populated when seeded; sensitive
  changed_paths are excluded with a correct `sensitive_excluded_count` and
  never leak into the bundle; `bundle_sha256` remains recomputable;
  `SensitivePathFilterTests` cover the helper patterns and false-positive
  guards. **Acceptance:** `list_tools()` includes `export_evidence_bundle`
  only when opted in.

#### M-EV-07 Nondeterminism detection + deterministic replay
- **Status:** DONE. **Evidence:** `verification.py:verify` runs each
  verification command `verification_deterministic_repetitions` times and
  compares runs via `_commands_diverge` (status / exit_code / stderr); a
  divergence sets `nondeterminism_labeled=True` and
  `infrastructure_status="DEGRADED"` in the structured result. The deterministic
  case (`infrastructure_status="HEALTHY"`, `nondeterminism_labeled=False`) is
  covered at repetitions=1 (M-EV-02 test) and, with this milestone, at
  repetitions=2 as the explicit deterministic control.
- **Build:** `verification.py:verify` — run a command's declared deterministic
  subset `deterministic_repetitions` times (M-CC-09); if results diverge, set
  `infrastructure_status=DEGRADED` and `nondeterminism_labeled=True` in the
  structured result. Replay: persist enough (env digest + command digests +
  base commit) to re-run offline; a read-only `replay_verification` is **NEW
  SURFACE** and remains intentionally deferred (not implemented in this
  milestone; no new surface shipped).
- **Tests:** `tests/test_verification.py` — `test_flaky_command_is_labeled_nondeterministic`
  (stderr diverges across 2 runs → labeled + DEGRADED) and
  `test_deterministic_command_not_labeled_at_repetitions_two` (identical runs at
  repetitions=2 → not labeled + HEALTHY). **Acceptance:** nondeterministic
  verification is honestly labeled in the structured result.

#### M-EV-08 Artifact retention + GC policy
- **Status:** DONE. **Evidence:** `config.py` adds `evidence_retention_days`
  (env `EVIDENCE_RETENTION_DAYS`, default 0 = disabled) and
  `checkpoint_retention_days` (env `CHECKPOINT_RETENTION_DAYS`, default 0 =
  disabled) via the existing `_non_negative_int` helper. `store.gc_artifacts`
  deletes only non-promoted, non-structural checkpoint (+ cascaded
  `job_checkpoint_detail`) rows older than `now - checkpoint_retention_days`;
  promoted evidence is never deleted. A checkpoint is protected if its
  `job_id` has a committed verification (`verifications.used = 1`), or any
  `reviews`/`publications` row, or it is directly referenced by
  `revision_resolves_finding.resolving_checkpoint_id`; structural kinds
  (`boundary`/`extension`/`milestone`) are always retained. Time is made
  testable via an optional `now_iso` cutoff param (store never reads the wall
  clock). An additive `idx_job_checkpoints_recorded_at` index backs the
  retention scan; `SCHEMA_VERSION` stays 1. `compact_checkpoints` is unchanged.
- **Build:** `config.py` — `EVIDENCE_RETENTION_DAYS` + `CHECKPOINT_RETENTION_DAYS`;
  a `store.py:gc_artifacts` that deletes only non-promoted checkpoint/detail
  rows older than retention, never `verifications`/`reviews`/`publications`/
  `syncs`. Document the invariant in `store.py`.
- **Tests:** `tests/test_checkpoints.py` `ArtifactGcTests` — GC deletes old
  sample checkpoints, keeps a promoted verification's checkpoints forever;
  structural (boundary/extension/milestone) and recent rows survive; unused
  verification does not protect; review/publication protect; resolution-link
  protects the referenced checkpoint; detail rows cascade; evidence tables
  are never touched; retention=0 is a no-op; additive index + schema version
  1. **Acceptance:** promoted evidence survives GC; no new tool.

### Release 3 — Governance

#### M-GV-01 Durable authorization objects
- **Status:** PARTIAL. **Evidence:** `server.py` handlers re-state evidence
  (`base_head`, `fingerprint`, ordered paths) inline per call; `models.py:13-20`
  `ProjectCapability` is a static flag, not a per-action authorization record
  with actor/authority/expiry/delegation. **Gap:** ChatGPT wants durable,
  target-specific authorization objects.
- **Build:** additive `authorizations` table:
  ```sql
  CREATE TABLE IF NOT EXISTS authorizations (
      authorization_id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL,
      job_id TEXT,
      actor TEXT NOT NULL,                 -- ChatGPT|human:<id>|external:<id>
      authority_source TEXT NOT NULL,      -- chatgpt_confirmation|policy|delegated
      capability TEXT NOT NULL,            -- commit|push|review|export|...
      target TEXT NOT NULL,                -- branch|fingerprint|paths digest
      issued_at TEXT NOT NULL,
      expires_at TEXT,
      single_use INTEGER NOT NULL DEFAULT 1,
      used INTEGER NOT NULL DEFAULT 0,
      delegated_from TEXT,                 -- parent authorization_id
      detail_json TEXT NOT NULL
  );
  ```
  A new `AuthorizationManager(settings, store)` issues/consumes/checks/expiry.
  Sensitive tool handlers (commit, push, review, export) require an unused,
  unexpired authorization for the capability + target, and mark it `used` in
  the same ACID transaction. Keep the inline-evidence path as the default until
  projects opt in, so behavior is unchanged for existing projects.
- **Tests:** `tests/test_authorizations.py` — issue→consume→used;
  single-use rejects reuse; expiry rejects; a different target rejects;
  delegation chain records `delegated_from`; `SCHEMA_VERSION` stays 1. **NEW
  SURFACE only if exposed as a tool** — keep it internal first (the manager is
  invoked from existing handlers), no new tool in this milestone.

#### M-GV-02 Named roles Planner / Evidence-officer / Supervisor
- **Status:** PARTIAL. **Evidence:** Implementer, Test-operator, Independent
  reviewer are DONE (`jobs._run`, `verification.verify`, `review.run_independent
  _review`). Planner, Evidence-officer, Supervisor are MISSING; Test-operator is
  already DONE (do not rebuild). **Gap:** three named roles missing.
- **Build:** reuse `claude.py` with role-specific command builders, mirroring
  `build_reviewer_command` (`review.py:581`): `build_planner_command`
  (read-only, turns a ratified requirement set into a bounded milestone plan —
  writes a plan object, no repo edits), `build_evidence_officer_command`
  (read-only, checks fingerprints/bindings/coverage and emits a verdict),
  `build_supervisor_command` (read-only, scans checkpoints for loops/drift/
  budget pressure and emits escalate/narrow/replace recommendations). Each is
  a *capability-gated invocation* on the existing job/reader infrastructure, not
  a new agent runtime. Planner output feeds the milestone list (M-CT-04);
  Evidence-officer output feeds the commit gate (M-GV-05); Supervisor output
  feeds the control surface (M-CC-08). **Internal first.**
- **Tests:** `tests/test_roles.py` — each role's command is read-only
  (`Read,Glob,Grep` only, no `Edit`/`Write`); Planner output parses to a
  milestone list; Evidence-officer rejects a fingerprint mismatch; Supervisor
  flags no-progress. **Acceptance:** roles run isolated and read-only; no new
  tool unless surfaced (defer).

#### M-GV-03 External grounding for genesis/apex authority
- **Status:** MISSING. **Evidence:** `models.py:82` `ProjectPolicy` is the
  highest authority and is self-contained in `projects.json`; nothing requires
  it to be grounded in an external ratified spec. **Gap:** ChatGPT wants
  external grounding for genesis/apex authority.
- **Build:** `models.py:ProjectPolicy` gains an optional
  `apex_authority_ref` (a path in the repo + a commit SHA, or a URL). The
  registry rejects a policy whose `apex_authority_ref` is missing when the
  project opts into `REQUIRE_APEX_GROUNDING` (a new capability-less flag, or
  reuse the existing `repository_read` enforcement). Genesis jobs (the first
  job on a project) require the ref to be present and unchanged since
  registration. **Tests:** `tests/test_registry.py` — a grounded policy
  registers; an ungrounded one is rejected when the flag is set; changing the
  apex ref without re-confirmation is rejected. **Acceptance:** genesis
  authority is externally grounded when opted in; no new tool.

#### M-GV-04 Spec-gated runtime under a ratified spec commit
- **Status:** MISSING. **Evidence:** `grep` for `ratif`/`specification commit`
  returns nothing. **Gap:** ChatGPT wants runtime implementation allowed only
  under a named ratified spec commit.
- **Build:** `models.py:ProjectPolicy` gains `ratified_spec_commit` (a SHA).
  When set, `jobs.py:create_development_job` requires the job's spec (the
  `instructions`/milestone source) to be pinned to that commit; a job whose
  spec drifts is rejected. This composes with M-GV-03 (apex authority is the
  spec; the ratified commit is the pinned point). **Tests:**
  `tests/test_jobs.py` — a job under a ratified spec is accepted; a spec drift
  is rejected. **Acceptance:** runtime work is spec-gated when opted in; no
  new tool.

#### M-GV-05 Self-certification prevention (internal guard)
- **Status:** PARTIAL. **Evidence:** `review.py:22-25` structurally prevents
  bridge-spawned reviewer self-certification (fresh non-resumable read-only
  session). But `record_candidate_review` (`review.py:520`) accepts a human/
  external review with no `reviewer ≠ drafter` guard. **Gap:** a human
  reviewer could certify their own draft.
- **Build:** `review.py:record_candidate_review` — require `reviewer_id` to
  differ from the job's drafter identity (recorded on the job, M-EV-05
  authority). If they match, reject with a clear error. Document that the
  bridge-spawned path is already structurally independent.
- **Tests:** `tests/test_review.py` — a self-review is rejected; an
  independent human review is recorded. **Acceptance:** no agent may certify
  its own draft; no new tool.

#### M-GV-06 Sub-agent delegation + parent/child task trees + delegation records
- **Status:** MISSING. **Evidence:** `grep` for `delegat`/`parent`/`child`
  returns nothing; `store.py` jobs table has no `parent_job_id`. **Gap:**
  ChatGPT wants bounded sub-agent delegation, parent/child task trees, and
  delegation records.
- **Build:** `models.py:DevelopmentJob` gains `parent_job_id: str|None` and
  `delegation_mandate_json: str|None` (mandate/inputs/outputs/authority). The
  `authorizations` table (M-GV-01) records `delegated_from`. `jobs.py:create`
  accepts a `parent_job_id` and bounds the child to the parent's authorized
  scope (paths ⊆ parent's changed set, capability ⊆ parent's). A parent may
  spawn a bounded number of children (`config.py:MAX_DELEGATIONS_PER_JOB`).
  The terminal report surfaces the parent/child tree. **NEW SURFACE** only if
  a `delegate` tool is exposed — keep internal first (parent job spawns
  children via the manager).
- **Tests:** `tests/test_jobs.py` — a child is bounded to the parent's scope;
  a child exceeding scope is rejected; the delegation record links
  parent→child; over-delegation is rejected. **Acceptance:** delegation is
  bounded and recorded; tree is queryable.

#### M-GV-07 Parallel verify + review + reviewer conflict reconciliation
- **Status:** PARTIAL. **Evidence:** verification and review are independent
  managers but run sequentially in the job lifecycle; `review.py:730`
  `validate_for_commit` accepts one review and has no conflict reconciliation.
  **Gap:** parallel execution and conflict resolution.
- **Build:** `jobs.py` — run `verify` and `run_independent_review`
  concurrently (`asyncio.gather`) once the candidate is stable; record both.
  `review.py` gains `reconcile_reviews(review_ids)` that, given divergent
  verdicts, requires a tie-breaker review (a third reviewer with a distinct
  `reviewer_id`) and records the resolution. The commit gate requires a
  reconciled pass. **Tests:** `tests/test_review.py` — divergent verdicts
  trigger a tie-breaker; a reconciled pass allows commit; an unresolved
  conflict blocks commit. **Acceptance:** parallel gates run; conflicts are
  reconciled; no new tool.

### Release 4 — Institutional (P3, deferred)

#### M-IN-01 P3 capability learning & reconstruction (deferred)
- **Status:** MISSING (all ten P3 items: Capability Capsules, Passports,
  recorded failure modes, reconstruction dossiers, blind-reconstruction
  experiments, portability checks, capability compatibility constraints,
  evidence-based agent selection, delta qualification, versioned lessons).
  **Evidence:** `grep` for `capsule`/`passport`/`dossier`/`blind_reconstruct`/
  `portability`/`delta_qualif`/`lesson` returns nothing.
- **Gap:** the full P3 institutional layer.
- **Build:** **do not build until releases 1–3 prove out and CKS-002B is
  unblocked.** When started, each P3 item is its own milestone (Capsules =
  additive `capability_capsules` table summarizing a successful mission into a
  reusable, signed lesson; Passports = additive `agent_passports` table of
  capability attestations; etc.). These are research-shaped and large; they
  are explicitly **non-goals for the unblock** (see Non-goals).
- **Acceptance:** a future release plan decomposes P3 into its own milestones
  once releases 1–3 are accepted.

## Cross-cutting (slotted by priority)

#### M-CC-01 PR creation + existing-PR discovery + update draft PR — **NEW SURFACE**
- **Status:** MISSING. **Evidence:** `grep` across `src/local_dev_mcp/` for
  `gh pr`/`pull` returns nothing; step 13 of the lifecycle is explicitly
  excluded today. **Gap:** ChatGPT's lifecycle step 13 (open/update a draft PR)
  and the two "Missing" items (existing-PR discovery, update-existing-PR).
- **Build:** **NEW** `ProjectCapability.OPEN_PULL_REQUEST` (requires `push
  _development_commit`); **NEW** tool `open_pull_request(project_id, job_id,
  base_head, fingerprint, title, draft=True)` that: (a) discovers an existing
  PR for the job's branch via `gh pr list --head <branch> --json number,url`;
  (b) if found, updates its title/body via `gh pr edit`; (c) else opens a new
  draft PR via `gh pr create --draft`. Re-stated evidence required; the PR
  base is the job's base branch; merge is prohibited. Dormant by default.
- **Tests:** `tests/test_pr.py` — discovery finds an existing PR and updates
  it; no existing PR → creates draft; merge command never issued; tool-count
  assertion bumps. **Acceptance:** `list_tools()` includes
  `open_pull_request` only when opted in.

#### M-CC-02 Confirm remote branch/PR state
- **Status:** PARTIAL. **Evidence:** `verification.py:1447-1453` `_push_and_
  verify` confirms the remote tip after push; no PR-state confirmation.
- **Build:** extend the publication result + `open_pull_request` to surface
  `remote_branch_state` (present/vanished, head SHA matches) and `pr_state`
  (open/closed/merged, number, url). Internal enrichment of existing
  snapshots; no new tool beyond M-CC-01.
- **Tests:** `tests/test_pr.py` — a vanished branch is reported; a merged PR
  is reported as merged. **Acceptance:** remote/PR state is queryable.

#### M-CC-03 Successor job from an immutable completed candidate
- **Status:** MISSING. **Evidence:** `grep` for `successor` returns nothing;
  `jobs.py:696-697` `resume` rejects a job with completed work but there is no
  successor path. **Gap:** ChatGPT wants a successor job from an immutable
  completed candidate.
- **Build:** `jobs.py` — `create_successor_job(parent_job_id, instructions)`
  creates a new job whose `base_head` is the parent's *published* commit (not
  the canonical HEAD), preserving the immutable parent. Recorded via the
  parent/child tree (M-GV-06). **Internal first.**
- **Tests:** `tests/test_jobs.py` — a successor branches off the parent's
  published commit; the parent is unchanged. **Acceptance:** successors are
  createable without mutating completed jobs; no new tool.

#### M-CC-04 Merge-vs-ratification + separate authz for merge/tag/release/deploy — **NEW SURFACE**
- **Status:** PARTIAL. **Evidence:** `jobs.py:87-96` `PROHIBITED_ACTIONS`
  blocks `merge`/`tag`/`deploy`/`force`/`ref-move`; commit/push are separate.
  **Gap:** no ratification act distinct from merge, and merge/tag/release/
  deploy are blocked entirely rather than separately authorized.
- **Build:** decide the product direction with the user before building.
  Option A (recommended, zero new surface): keep merge/tag/release/deploy
  prohibited; add a `ratify_candidate` *internal* act that records governance
  ratification (a `ratifications` sidecar table) distinct from a future merge
  the human performs outside the bridge. Option B (new surface): a
  `merge_pull_request` capability + tool, separately authorized, opt-in,
  non-trunk, no-force — only if the user explicitly wants the bridge to merge.
  **Do not implement B without explicit user request** (security constraint).
- **Tests:** Option A — a ratification record is created and is distinct from
  any merge. **Acceptance:** ratification ≠ merge; merge stays prohibited
  unless/until the user authorizes Option B.

#### M-CC-05 Branch/PR race-safe idempotency
- **Status:** PARTIAL. **Evidence:** `verification.py:1599-1647` `publish` is
  branch-idempotent; PR creation is not yet present to be idempotent.
- **Build:** ensure `open_pull_request` (M-CC-01) and any PR update are
  idempotent under concurrent calls (discover-then-create/update; tolerate a
  race where another caller created the PR between discover and create).
  **Tests:** `tests/test_pr.py` — two concurrent `open_pull_request` calls
  produce exactly one PR. **Acceptance:** no duplicate PRs under races.

#### M-CC-06 Unexpected-files / generated-artifact / submodule detection
- **Status:** PARTIAL. **Evidence:** `verification.py:502-505` rejects
  submodules; no general unexpected-files/generated-artifact detection.
- **Build:** `verification.py` — a configurable `unexpected_paths` policy per
  project (e.g. block `*.lock`, `dist/`, `node_modules/`, generated artifacts
  not in the declared changed set). Flag, and reject at the commit gate if
  declared. **Tests:** `tests/test_verification.py` — an unexpected artifact
  is rejected; a declared one is allowed. **Acceptance:** no new tool.

#### M-CC-07 Read-only completed-worktree recovery
- **Status:** MISSING. **Evidence:** `grep` for `recover.*worktree`/read-only
  recovery returns nothing; `jobs.py:753-777` `cancel` cleans up but does not
  recover a completed worktree read-only. **Gap:** ChatGPT wants read-only
  recovery of a completed-job worktree.
- **Build:** `worktrees.py` — `recover_completed_worktree(job_id)` re-attaches
  a vanished completed worktree from the content-addressed candidate
  (checkout the published commit detached, verify fingerprint), read-only.
  **Internal first.** **Tests:** `tests/test_worktrees.py` — a recovered
  worktree matches the fingerprint and is read-only. **Acceptance:** completed
  work is recoverable read-only; no new tool.

#### M-CC-08 Human control surface (dashboard enrichment + control tools)
- **Status:** PARTIAL. **Evidence:** dashboard fields in `_snapshot`
  (`base_head`, `candidate_fingerprint`, `changed_paths`, `budget`, blocked
  conditions); controls: resume DONE, scoped approval DONE, request-another
  reviewer DONE. **MISSING:** `requirements_covered`, `authorities_in_force`,
  and the controls `pause`/`extend`/`narrow`/`replace worker`/`discard`/
  `revoke authorization`/`export evidence`. **Gap:** most of the control surface.
- **Build:** Two tracks. (1) **Dashboard enrichment (internal):** add
  `requirements_covered` (M-EV-01) and `authorities_in_force` (M-GV-01) to
  `_snapshot`. (2) **Control tools (NEW SURFACE, opt-in per control):** each
  control is its own capability-gated tool with `ToolAnnotations` —
  `pause_development_job` (idempotent, sets a paused state), `extend_budget`
  (bounded, records an authorization), `narrow_scope` (reduces a job's
  allowed paths), `replace_worker` (re-spawns the drafter on the same
  candidate), `discard_candidate` (removes the worktree, records the
  discard), `revoke_authorization` (marks an authorization `used`/expired),
  `export_evidence` (= M-EV-06). Each dormant by default. Build incrementally;
  `export_evidence` and `pause` first (highest unblock value).
- **Tests:** `tests/test_controls.py` — each control is capability-gated,
  idempotent where claimed, and records an audit event; tool-count assertion
  bumps per added tool. **Acceptance:** each control appears in `list_tools()`
  only when opted in; dashboard carries the new fields.

#### M-CC-09 Verification-lab determinism
- **Status:** PARTIAL. **Evidence:** `verification.py:_run_command` sets
  `LANG=C.UTF-8`, `LC_ALL`, `PYTHONIOENCODING`, wall-clock timeout; missing
  pinned deps, cached deps, network policy, deterministic clock/TZ/random,
  fs order, flaky detection, baseline comparison, signed results.
  **Gap:** the hermetic verification lab.
- **Build:** `verification.py` — `PIN_TOOLCHAIN` (a pinned venv/lockfile per
  project, used by `_run_command`), `DEPS_CACHE_DIR` (read-only cached deps,
  not writable by commands), `NETWORK_POLICY` (none|allowlist, enforced by
  running commands with no network namespace or an allowlist), deterministic
  `TZ=UTC`, `SOURCE_DATE_EPOCH`, `PYTHONHASHSEED=0`, sorted fs iteration where
  the bridge lists files. `deterministic_repetitions` (M-EV-07) for flaky
  detection; `baseline_comparison` against the last green verification's
  fingerprint. **Internal first**; a `replay_verification` tool is **NEW
  SURFACE** (defer).
- **Tests:** `tests/test_verification.py` — a pinned toolchain is used; a
  network-blocked command fails closed; a flaky command is labeled; a
  baseline mismatch is flagged. **Acceptance:** verification is hermetic when
  opted in; no new tool in this milestone.

#### M-CC-10 Failure-recovery hardening
- **Status:** PARTIAL. **Evidence:** `jobs.py` handles no-progress, tool-
  failure, extension; `verification.py` handles candidate corruption
  (re-derive); `worktrees.py:validate_resume` handles lost base; `jobs.py:
  47-48` defines `QUARANTINED_PROVENANCE` but never enters it. **Gap:** the
  full P2 failure-recovery matrix.
- **Build:** `jobs.py` — tool-failure retry within policy then isolate as
  `FAILED_INFRASTRUCTURE`; candidate-corruption rollback to last verified
  checkpoint (reset the worktree to the checkpointed partial fingerprint's
  paths); lost-worktree reconstruction from the content-addressed patch
  (re-apply the checkpoint changed-set on a fresh detached worktree);
  compromised-provenance quarantine machinery (enter `QUARANTINED_PROVENANCE`
  on a fingerprint mismatch detected post-verification, M-CT-05);
  immutable-completed-job + successor (M-CC-03). **Internal first.**
- **Tests:** `tests/test_recovery.py` — tool-failure retries then isolates;
  corruption rolls back; a lost worktree is reconstructed; compromised
  provenance quarantines (non-resumable). **Acceptance:** each failure mode
  recovers or isolates per the P2 matrix; no new tool.

#### M-CC-11 Security hardening
- **Status:** PARTIAL. **Evidence:** path redaction, protected paths,
  worktree isolation, minimal env, no-shell, origin-only push, no-silent-
  access-expansion are DONE. **MISSING:** per-job fs/network/credential/tool
  policy, least-privilege short-lived credentials, supply-chain scanning,
  prompt-injection defenses, signed manifests/evidence, sensitivity
  classification before export, sandboxed repo-controlled-script execution.
- **Build:** `models.py:ProjectPolicy` — per-job `filesystem_policy` (read/
  write allowlists), `network_policy` (carried into M-CC-09), `credential_
  policy` (short-lived, least-priv, projected into the worktree only);
  `supply_chain_scan` (run a configured scanner over changed deps before
  commit, reject on high-severity); `prompt_injection_defense` (strip/
  fence untrusted content the reader ingests); `signed_manifests` (sign the
  evidence bundle, M-EV-06, with a project key); `sensitivity_classification`
  (classify artifact sensitivity before export, gating M-EV-06 inclusion);
  sandboxed repo-controlled-script execution (run `verification_commands`
  that are repo-controlled in a sandbox, not the bridge's cwd). Each is a
  project opt-in; most are **internal**, a few may need a **NEW SURFACE** tool
  (e.g. `sign_evidence`); flag per-item when scoping.
- **Tests:** `tests/test_security.py` — a sensitive artifact is redacted
  from the bundle; a repo-controlled script runs sandboxed; a supply-chain
  hit blocks commit. **Acceptance:** each hardening item is opt-in and
  tested; new tools flagged.

## Minimum unblock checklist (CKS-002B restart)

Mapping the roadmap's `minimum_unblock` to the milestones above. A release
is unblocked when every `required` row is DONE.

| `minimum_unblock` item | Milestone | Status |
|---|---|---|
| `canonical_sync` | (MCPB-009, merged) | DONE |
| `pinned_base` | M-CT-01 | DONE (2026-08-26) |
| `resumable_checkpoint` | (MCPB-010) | DONE |
| `soft_turn_limit:30` / `extension_block:30` / `hard_turn_limit:120` / `max_no_progress_turns:6` | (MCPB-010) | DONE |
| `compaction` | (MCPB-010) | DONE |
| `milestone_plan` | M-CT-04 | PARTIAL → R1 (snapshot partition + acceptance_command tests) |
| `requirement_traceability` | M-EV-01 | PARTIAL → R2 (module exists; wire into jobs/snapshot; full matrix) |
| `fixed_verification_binding` | (MCPB-005) | DONE |
| `independent_review_binding` | (MCPB-007) | DONE |
| `candidate_fingerprint` | (MCPB-005) | DONE |
| terminal states `COMPLETE/PAUSED_BUDGET_EXHAUSTED/BLOCKED_INPUT_REQUIRED/FAILED_CANDIDATE/FAILED_INFRASTRUCTURE/QUARANTINED_PROVENANCE` | M-CT-05 (BLOCKED_INPUT_REQUIRED, QUARANTINED_PROVENANCE) | PARTIAL → R1 (QUARANTINED_PROVENANCE unreachable) |

**Conclusion:** the only CKS-002B blockers remaining are M-CT-04 (atomic
milestones — PARTIAL), M-CT-05 (the two dormant states — PARTIAL,
`QUARANTINED_PROVENANCE` unreachable), and M-EV-01 (requirement traceability —
PARTIAL, not wired in). M-CT-01 is now DONE. Everything else is hardening or
vision.

## Recommended next action (2026-08-28)

1. **The entire roadmap R1–R3 + Cross-cutting is now DONE, including the three
   former PARTIAL rows.** MCPB-001 → 012, M-AL-01, all R1 (M-CT-02…07), all R2
   (M-EV-01…08), all R3 (M-GV-01…07), and all M-CC-01…11 shipped. The 2026-08-27
   integration cherry-picked all 22 capability commits onto `main` in
   dependency order; the 2026-08-28 PARTIAL-closure tail added the
   `replay_verification` read-only tool (39 tools, 22 capabilities),
   `VerificationCommand.kind` test-kind coverage, and the internal
   `static_analysis` block; the full suite is green (**591 tests**).
2. **Release.** Version stays **0.11.0** (the release commit `aa1b7b0` was never
   pushed). The remaining work is release mechanics: merge `origin/main`,
   push, tag `v0.11.0`, and a GitHub release — each step is an explicit
   user-authorization action.
3. **No capability gaps remain to close.** Future work is optional hardening:
   - **M-IN-01 (P3)** stays a non-goal (explicitly deferred).
   - Optional: run a fresh audit of the 149-item table against the merged
     `main` if new gaps are suspected.
4. The build discipline recorded here is now historical: implement → run the
   full core suite (`PYTHONPATH=src .venv/bin/python -m unittest discover -s
   tests -v`) → inspect → commit (AGENTS.md §3); NEW SURFACE milestones add a
   `ProjectCapability` + `ToolAnnotations`, dormant by default, and bump the
   tool-count canary (now 39).

## Non-goals (explicitly out of scope for the unblock)

- **P3 / Institutional release (M-IN-01):** Capsules, Passports, dossiers,
  blind reconstruction, delta qualification. Research-shaped and large; do not
  block CKS-002B on this.
- **A manual `pause` tool as a hard requirement:** auto-pause + resume
  (MCPB-010) is the unblock; the `pause` *control tool* (M-CC-08) is opt-in
  hardening, not a blocker.
- **Option B merge/tag/release/deploy tools (M-CC-04):** these stay
  prohibited unless the user explicitly authorizes a new surface. The bridge
  ratifies; a human merges.
- **Parallel milestone execution:** M-CT-04 tracks milestones; it does not
  parallelize slices. Parallelism is deferred.
- **A UI/dashboard:** the bridge exposes MCP tools to ChatGPT; enrichment is
  additive fields in existing snapshots, not a separate UI.

## How to resume from here

1. Read `docs/MCPB-ROADMAP.md` (this file) first — it is the single source of
   truth for what is closed, what remains, and the build order. As of the
   2026-08-27 integration, **R1–R3 + Cross-cutting are all DONE**; no
   capability gaps remain open (M-IN-01/P3 stays a non-goal).
2. Check `AGENTS.md` "Current resume pointer" for the phase pending
   acceptance, and the canonical accepted-sequence table for what is merged.
3. Check the memory file `mcpb-roadmap-and-state.md` for the current PR state
   and the next gap.
4. If the user asks for new work, branch off `main` and follow the
   additive-table + lazy-import + zero-surface pattern, adding a
   `test_<area>_adds_no_new_mcp_tools` assertion unless the milestone is
   flagged **NEW SURFACE** (in which case add a new `ProjectCapability`,
   `ToolAnnotations`, dormant-by-default, and bump the tool-count assertion,
   currently 38). Run the full suite
   (`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v`) before
   committing.
5. Preserve the verbatim security constraints above; route any new push
   surface through the opt-in `push_development_commit` / `open_pull_request`
   path (non-trunk, origin-only, no-force); ChatGPT confirmation remains the
   outer authorization gate.