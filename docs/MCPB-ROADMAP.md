# BridgeLab capability roadmap

> Consolidated status of every capability discussed in the source-of-truth
> planning conversation (`~/.claude/plans/we-need-to-add-jaunty-emerson.md`),
> updated after MCPB-010. Use this to orient a new session: it says what is
> built, what is merged, what is open, and which gap to close next.
>
> The **canonical accepted sequence** and per-phase decision records live in
> `AGENTS.md` and `docs/MCPB-0NN.md` / `docs/MCPB-0NN-IMPLEMENTATION-BRIEF.md`.
> This doc is the forward-looking index; `AGENTS.md`'s "Current resume pointer"
> names the phase currently pending acceptance.

## Current state (as of 2026-08-07)

- **MCPB-001 → MCPB-009 are merged to `main`** (remote `origin/main` at
  `2a8ad1d`). Phase branches were stacked feature branches merged in order:
  - PR #1 — MCPB-001→006 → `main`
  - PR #3 — MCPB-007 (+ the MCPB-008 commit on the same branch) → `main`
  - PR #4 — MCPB-009 → `main`
- **MCPB-010 — Continuity release is open as PR #5**
  (`feature/mcpb-010-continuity` → `main`), pending acceptance. Committed at
  `09aca52`; package version `0.10.0`; 167 tests pass; **zero new MCP tools**.
  Decision record: `docs/MCPB-010.md`; brief:
  `docs/MCPB-010-IMPLEMENTATION-BRIEF.md`.
- **Nothing past MCPB-010 is implemented.** Everything below this line is
  future work, not yet built or committed.
- Local `main` may be stale; run `git fetch --prune origin` before any
  base-branch reasoning. Per-phase feature branches are kept until merged.

## What MCPB-010 closed (was the P0 unblock)

These were MISSING/PARTIAL before and are now DONE — do **not** rebuild them:

- Durable turn-level mission state (`job_checkpoints` / `job_events` /
  `job_progress`, additive, `SCHEMA_VERSION` stays 1).
- Resumable `paused_*` states + terminal `failed_infrastructure` /
  `quarantined_provenance`; crash-with-checkpoints → `paused_interrupted`.
- Soft / multi-dimensional / progress-gated budgets with in-place
  `--resume <session_id>` extension slices.
- Resume from the last checkpoint (keeps cumulative budget, seeds the
  drafter).
- Checkpoint compaction.
- Opt-in `CLAUDE_STREAM_JSON` turn counting + a pure `_parse_output`.
- Structured terminal report in `get_development_job` (`prohibited_actions`,
  `budget`, `checkpoint_count`, `milestones`, `blockers`, `pause_reason`,
  `candidate_fingerprint`, `changed_paths`).
- Lightweight milestone tracking (tracked, not parallelized).

## Recommended build order (from the approved plan)

### Release 2 — Evidence (the next release to plan)

Half of the evidence core already exists (content-addressed candidate/commit/
publication fingerprints, single-use verification IDs, fingerprint re-check,
reviewer independence). The missing half:

| Capability | Status | Notes |
|---|---|---|
| Append-only **job** event ledger | **PARTIAL** | `job_events` exists but is job-scoped; the plan's goal is the event ledger as the **audit source of truth**, reducing reliance on in-place `jobs` upserts. Decide whether to promote it or keep the current hybrid. |
| Environment / toolchain fingerprint | **MISSING** | Add `environment_digest` to `VerificationRecord`; pin/digest the verification toolchain so a verification is reproducible and labeled for nondeterminism. |
| Structured `verification:{...}` result schema | **MISSING** | Replace the flat `verify()` dict with the roadmap's structured result object (commands, exit codes, output bounds, environment digest, nondeterminism label). |
| Review content hash + reviewer-independence class | **MISSING** | Reviews carry no content hash; formalize the reviewer-independence class as a bound artifact. |
| **Requirement traceability** | **MISSING** | New `requirements.py`: reader-based normative-ID extraction → requirement-to-code-to-test matrix; additive `requirements` table. **Directly serves CKS-002B — the highest-value item here.** |
| Portable evidence bundle | **MISSING** | New read-only `export_evidence_bundle` tool (this release's one likely new tool surface — needs its own authorization). |
| Source classifications / authority-behind-instructions / revision→finding links / deterministic replay / nondeterminism labeling | **MISSING** | Longer-tail evidence plumbing; sequence after the above. |

**Recommended next gap to close:** requirement traceability (`requirements.py`
+ `requirements` table), because it is the stated CKS-002B blocker and reuses
the existing reader + additive-table patterns. Pair it with the structured
`verification:{...}` schema so the traceability matrix binds to real evidence.

### Release 3 — Governance

The core authz engine exists (capability gating, single-use IDs, commit/push
separation, exact-evidence binding, ratification-distinct-from-merge). Missing
authority model:

| Capability | Status | Notes |
|---|---|---|
| **Durable authorization objects** | **MISSING** | New additive `authorizations` table: actor identity, authority source, scope, effective time, expiry, delegation chain, single-use-vs-reusable. Checked per sensitive tool call instead of (or alongside) inline repeated-evidence params. |
| Named roles | **MISSING** | Planner / Test-operator / Evidence-officer / Supervisor as distinct, capability-gated invocations — reuse `claude.py` with role-specific command builders mirroring `build_reviewer_command` (not a new agent runtime). |
| Actor identity / role / authority source / expiry / delegation chain | **MISSING** | Backing fields for the above. |

### Release 4 — Institutional (P3, deferred)

**Do not block Cairn on this.** Only after releases 2–3 prove out:

- Capsules, Passports, dossiers, blind reconstruction, delta qualification,
  capability learning & reconstruction. Speculative and large.

## Cross-cutting gaps (slotted by priority, not a release)

These are valuable but orthogonal; pick them up when a release touches the
relevant area.

- **PR creation (lifecycle step 13) + successor-job-from-completed-candidate** —
  the most valuable Git-lifecycle gaps. Explicitly excluded today; opening a
  draft PR needs its own authorization (separate from push).
- **Existing-PR discovery / update-existing-PR** — missing.
- **Merge-vs-ratification distinction + separate authorization for
  PR/merge/tag/release/deploy** — only commit/push/sync exist today.
- **Verification-lab determinism (P2)** — environment digest, deterministic
  repetitions / flaky detection, infra-vs-candidate-failure separation, network
  policy, pinned toolchain, deterministic clock/TZ/random. Overlaps with the
  Evidence release's environment fingerprint + structured `verification:{}`
  schema.
- **Failure-recovery hardening (P2)** — tool-failure retry+isolate (MCPB-010
  added the `failed_infrastructure` *state* but not retry/isolation),
  lost-worktree reconstruction from a content-addressed patch, compromised-
  provenance quarantine machinery (state exists; machinery does not),
  immutable completed job + formally linked successor job.
- **Atomic milestone parallelization (P2)** — MCPB-010 tracks milestones; it
  does not parallelize them.
- **Agent orchestration (P2)** — sub-agent delegation, parent/child task trees,
  parallel verify+review, reviewer conflict reconciliation. Named roles are
  in the Governance release.
- **Progress intelligence (mostly done in MCPB-010)** — per-turn progress
  capture, stop/escalation triggers, bounded-extension-when-advancing,
  elapsed/remaining/confidence tracking. Remaining: confidence/elapsed
  tracking and richer escalation triggers (oscillating edits, weakened tests).
- **Security hardening (P2)** — per-job network/credential policy, supply-chain
  scanning, prompt-injection defenses, signed manifests/evidence, sensitivity
  classification before export, sandboxed repo-controlled-script execution.

## Non-goals (explicitly excluded, do not reopen without a new decision)

- A manual `pause` tool (continuity is internal; auto-pause + resume is the
  unblock). A pause tool would add a surface needing its own authorization.
- Force-push, merge-commit creation, tag, remote trunk update, deletion, or a
  configurable sync/push remote (`origin` only). The bridge never directly
  edits its production checkout.
- Directly editing the production checkout — always via candidate worktrees.
- A schema-version bump for additive work (`SCHEMA_VERSION` stays 1).
- Institutional/P3 work blocking Cairn.

## How to resume from here

1. `git fetch --prune origin`; confirm PR #5 (MCPB-010) state.
2. After MCPB-010 is accepted/merged, the next release to **plan in detail** is
   the **Evidence release**, starting with **requirement traceability**
   (`requirements.py` + additive `requirements` table) and the structured
   `verification:{...}` result schema — both directly serve CKS-002B.
3. Follow the established pattern: read-only gap analysis against the real
   codebase → approved plan (`EnterPlanMode`/`ExitPlanMode`) → additive
   tables/helpers → `unittest` with fake executors → assert zero unintended
   tool-surface change → `docs/MCPB-0NN.md` + implementation brief +
   README/AGENTS.md + version bump → commit/push/PR only on explicit
   authorization.
4. Preserve every security constraint in `AGENTS.md` "Non-negotiable security
   boundaries" verbatim.