# BridgeLab Capability Elevation Implementation Progress

## Overview
This document tracks progress on implementing the capability elevations outlined in `docs/MCPB-CAPABILITY-ELEVATION.html` to elevate BridgeLab to "Super Developer Level".

## Completed Work

### M-EV-01: Requirement Traceability (Completed)
- **Status**: Completed
- **Description**: Implemented requirement traceability as the highest-value single unblock
- **Implementation**: 
  - Created `src/local_dev_mcp/requirement.py` with `RequirementManager` class
  - Follows spec-kit model: intent → spec (normative requirement IDs) → implementation → tasks
  - Extracts normative IDs using:
    1. Regex pattern: `^[A-Z]+-\d+[A-Z]?-R\d+`
    2. Fenced normative blocks: ```normative ... ```
  - Classifies content as normative vs example/commentary using contextual heuristics
  - Maintains additive schema changes:
    - `requirements` table: requirement_id, project_id, text, source_path, source_line, normative
    - `requirement_links` table: links requirements to jobs, changed paths, test names
  - Internal-first implementation: surfaces requirements_covered in existing snapshots
  - Prepares for future coverage-report tool (NEW SURFACE)
  - Uses lazy-import and zero-surface discipline
- **Files Modified**:
  - Created: `src/local_dev_mcp/requirement.py`
  - Updated: `src/local_dev_mcp/__init__.py` (export RequirementManager)
  - Updated: `src/local_dev_mcp/store.py` (added requirements and requirement_links tables)

## Pending Tasks

Based on the elevation map and recommended adoption order:

### 2. M-CT · MCPB-010 tail: Resumable state done better — checkpointers, stores, time-travel
- **Status**: Completed
- **Description**: Finish the continuity tail (fetch+pin base, atomic milestones, dormant states)
- **Pattern Source**: langchain-ai/langgraph
- **Key Elements**:
  - LangGraph's Checkpointers (short-term thread snapshots) + Stores (long-term cross-thread memory)
  - Time-travel, interrupts, subgraphs
  - BridgeLab's job_checkpoints as checkpointer; add long-term Store for cross-job lessons
  - Time-travel maps to atomic-milestone acceptance
  - Internal-first, no new tool
- **Implementation**:
  - **M-CT-01** (worktree base fetch + pin): `worktrees.py` gains `_resolve_remote_default_tip` (fixed-argv `git fetch origin` + `symbolic-ref` + `rev-parse`) and `create(..., requested_base=...)`; a requested base must equal the fetched `origin/<default>` tip or the job fails closed. Threaded through `DevelopmentJob.requested_base`, `start()`, and the `send_to_local_dev` tool's optional `requested_base` param (40-hex regex). The canonical checkout is left untouched.
  - **M-CT-04** (atomic milestones): `jobs.py` gains `_close_milestones` — a milestone dict with `acceptance_paths` (all must appear in the changed set) and optional `acceptance_command_id` (a verification command that must pass) closes only when both hold; closed milestones are stamped `closed`/`closed_at` and recorded as a `milestone` checkpoint. Wired into the `_run` boundary checkpoint.
  - **M-CT-05** (dormant states): `_blocked_input_signal` detects a fenced `<!-- bridge: blocked-input ... -->` block or `blocked_input` field and transitions the job to `BLOCKED_INPUT_REQUIRED` (resumable) instead of a candidate failure. `QUARANTINED_PROVENANCE` remains defined/non-resumable; its trigger is deferred to M-CC-10 per the roadmap.
- **Files Modified**:
  - Updated: `src/local_dev_mcp/worktrees.py`, `src/local_dev_mcp/jobs.py`, `src/local_dev_mcp/server.py`
  - Created: `tests/test_mct.py` (6 tests)

### 3. M-CC-09: Verification-lab determinism — the hermetic build
- **Status**: Completed
- **Description**: First open-source adoption adding runtime dependencies
- **Pattern Source**: Nix · devbox · nextest · Bazel · libfaketime
- **Key Elements**:
  - PIN_TOOLCHAIN via Nix flakes or devbox (bit-for-bit reproducible envs)
  - Flaky detection via deterministic repetitions (nextest's retry-classification model)
  - Deterministic env: TZ=UTC, SOURCE_DATE_EPOCH, PYTHONHASHSEED=0, sorted fs iteration, optional libfaketime
  - Baseline comparison against last green verification's fingerprint
  - Project opt-in; internal, fixed-argv
  - Future: replay_verification tool (NEW SURFACE)
- **Implementation**:
  - **Deterministic env**: `verification.py:_run_command` now sets `TZ=UTC`,
    `SOURCE_DATE_EPOCH=0`, and `PYTHONHASHSEED=0` so the same candidate +
    command always yields the same result (the hermetic lab). The reader already
    does sorted fs iteration.
  - **Flaky detection**: `verify()` runs each command `deterministic_repetitions`
    times (`VERIFICATION_DETERMINISTIC_REPETITIONS`, default 1); a divergent
    status/exit-code/stderr labels the verification `nondeterminism_labeled`
    and sets `infrastructure_status=DEGRADED` (else HEALTHY).
  - **Baseline comparison**: `store.load_last_passed_record` finds the last
    green verification for a project; `verify()` surfaces `baseline_fingerprint`
    and `baseline_regression` when the candidate differs from it.
  - Internal-first; no new tool.
- **Files Modified**:
  - Updated: `src/local_dev_mcp/verification.py`, `src/local_dev_mcp/config.py`,
    `src/local_dev_mcp/store.py`, `tests/test_config.py`
  - Created: `tests/test_verification_lab.py` (4 tests)

### 4. M-CC-11 · M-EV-06: Evidence bundle + supply-chain attestation
- **Status**: Completed (evidence bundle; supply-chain scan still pending)
- **Description**: Portable evidence bundle, source classifications, signed manifests, supply-chain scan
- **Pattern Source**: in-toto · SLSA · cosign · osv-scanner · syft
- **Key Elements**:
  - Bundle format: in-toto ITE-6 Bundle (.intoto.jsonl) - JSON-Lines of DSSE-wrapped Statements
  - Provenance: SLSA provenance/v1 predicate
  - Signing: sigstore-python's sign_dsse(Statement) → Bundle / verify_dsse(bundle, offline=True)
  - SBOM: CycloneDX with native in-document signatures (JSF) + BOM-Link
  - Drop: classic in-toto layout+links, SPDX, W3C VC, OCI/ORAS
  - export_evidence_bundle tool (NEW SURFACE, capability EXPORT_EVIDENCE_BUNDLE)
  - Internal-first; sensitivity classification gates inclusion
- **Implementation** (evidence bundle part):
  - **NEW** `ProjectCapability.EXPORT_EVIDENCE_BUNDLE` (requires `repository_read`).
  - **NEW** `evidence.py` `EvidenceBundleManager` + `export_evidence_bundle` MCP tool
    (dormant by default; runtime capability-gated via `resolve`).
  - Assembles a single redacted, content-addressed JSON bundle: candidate
    fingerprint + changed paths + diff summary, verification records + bindings,
    review records + verdict hashes, checkpoint summary, and a bundle-wide
    SHA-256 computed over the returned (redacted) bytes so a recipient can
    recompute it. Paths redacted; never exposes raw worktree paths/secrets.
  - Additive store helpers `load_record_rows_for_job` / `load_review_rows_for_job`.
  - Tool count 22 → 23 (guard tests in test_checkpoints/test_persistence updated).
- **Files Modified**:
  - Created: `src/local_dev_mcp/evidence.py`, `tests/test_evidence_bundle.py`
  - Updated: `src/local_dev_mcp/server.py`, `src/local_dev_mcp/models.py`,
    `src/local_dev_mcp/store.py`, `src/local_dev_mcp/__init__.py`,
    `tests/test_server.py`, `tests/test_checkpoints.py`, `tests/test_persistence.py`
- **Remaining** (supply-chain scan, M-CC-11): deferred — needs a runtime scanner
  (syft/grype/pip-audit) and is a separate project opt-in.

### 5. M-GV-06: Sub-agent delegation + parent/child task trees
- **Status**: Completed
- **Description**: Bounded parent/child task trees with delegation mandates + records
- **Pattern Source**: LangGraph subgraphs · crewAI roles · openai-agents handoffs
- **Key Elements**:
  - Add parent_job_id + delegation_mandate_json to DevelopmentJob
  - Bound children via config.MAX_DELEGATIONS_PER_JOB
  - Record delegation in authorizations table (delegated_from)
  - Internal-first; delegate tool is NEW SURFACE (defer)
- **Implementation**:
  - `DevelopmentJob` gains `parent_job_id` and `delegation_mandate` (persisted
    on the additive `job_progress` sidecar, so the `jobs` schema is untouched).
  - New additive `delegations` ledger table (parent_job_id, child_job_id,
    project_id, mandate_json, created_at) + store helpers
    `insert_delegation_row` / `load_delegation_rows_for_parent` /
    `load_delegation_rows_for_child`.
  - `DevelopmentJobManager.delegate(project, parent_job_id, text, effort,
    mandate)` spawns a bounded child: same project, real parent, and the parent
    must not exceed `MAX_DELEGATIONS_PER_JOB` (default 3). The delegation is
    recorded in the ledger and the child's parent/mandate are persisted.
  - Scope enforcement (fail-closed): a mandate must declare a non-empty
    `outputs` list; at completion a child whose changed paths fall outside its
    declared outputs (or that produces no verifiable changed paths) is rejected
    as `failed` rather than promoted. A child may not delegate further (one-level
    depth bound). Mandate structure is validated (`inputs`/`outputs`/`authority`
    only, bounded size).
  - The terminal report (`_snapshot`) surfaces `parent_job_id`,
    `delegation_mandate`, and the `children` tree from the ledger.
  - Internal-first: no new MCP tool (a `delegate` tool is deferred as NEW
    SURFACE).
- **Files Modified**:
  - Updated: `src/local_dev_mcp/jobs.py`, `src/local_dev_mcp/store.py`,
    `src/local_dev_mcp/config.py`, `tests/test_config.py`
  - Created: `tests/test_delegation.py` (6 tests)

### 6. M-GV-01/02/03/04: Durable authorization + named roles + external grounding + spec-gating
- **Status**: Completed (M-GV-01 durable authorization; M-GV-02/03/04 deferred)
- **Description**: Replace inline-evidence re-statement with durable authorizations table
- **Pattern Source**: OPA/Cedar · crewAI · spec-kit · MCP ToolAnnotations
- **Key Elements**:
  - Durable authorizations table (actor/authority_source/capability/target/issued_at/expires_at/single_use/delegated_from)
  - crewAI's role pattern: Planner/Evidence-officer/Supervisor as capability-gated invocations
  - spec-kit's spec-as-source-of-truth + ratified_spec_commit for external grounding and spec-gating
  - Internal-first; no new tool
  - Composes with M-EV-05 once authorizations table exists
- **Implementation** (M-GV-01):
  - New additive `authorizations` table (SCHEMA_VERSION stays 1) + store helpers
    `insert_authorization_row` / `load_authorization_row` /
    `mark_authorization_used` (atomic `WHERE used = 0` guard, returns bool).
  - New `authorization.py` `AuthorizationManager`: `issue` / `check` /
    `consume` / `check_and_consume` / `get`. `check` validates project,
    capability, target, unused, unexpired. `check_and_consume` is the
    TOCTOU-safe single-use path (atomic UPDATE, only the first concurrent
    consumer wins). Delegation chains record `delegated_from`.
  - Internal-first: no new MCP tool; the manager is exported but not yet wired
    into handlers (the inline-evidence path stays the default).
- **Files Modified**:
  - Created: `src/local_dev_mcp/authorization.py`, `tests/test_authorizations.py`
  - Updated: `src/local_dev_mcp/store.py`, `src/local_dev_mcp/__init__.py`
- **Remaining** (M-GV-02/03/04): named roles (Planner/Evidence-officer/
  Supervisor), external apex grounding, and spec-gated runtime — deferred to a
  later pass.

### 7. M-GV-07: Parallel verify + review + conflict reconciliation
- **Status**: Completed (reconcile_reviews; parallel fan-out deferred)
- **Description**: Parallel fan-out verification and review with conflict reconciliation
- **Pattern Source**: LangGraph fan-out/fan-in · Qodo multi-agent review
- **Key Elements**:
  - Once candidate stable, asyncio.gather verify + independent review concurrently
  - Qodo's multi-agent review model: separate review/improve/describe agents with PR-history indexing
  - Divergent verdicts trigger tie-breaker third reviewer with distinct reviewer_id
  - Commit gate requires reconciled pass
  - Additive reconcile_reviews(review_ids); no new tool
- **Implementation**:
  - `review.py` gains `reconcile_reviews(project, job_id, review_ids)` and a
    `_verdict_recommendation` helper (fail-closed normalization: only
    approve/pass/approved/accept/accepted → pass; anything else → fail).
  - Agreeing reviews reconcile to pass/fail; divergent verdicts require a
    **bridge-attested** tie-breaker (`_BRIDGE_REVIEWER_ID`, minted only by
    `run_independent_review`) — caller-supplied `reviewer_id` distinctness is
    NOT accepted as evidence of distinct reviewers (a single actor could forge
    labels). Returns a structured reconciliation result.
  - Internal-first: no new MCP tool.
- **Files Modified**:
  - Updated: `src/local_dev_mcp/review.py`, `tests/test_review.py`
- **Remaining** (parallel fan-out): running verify + review concurrently via
  `asyncio.gather` in the job lifecycle, and wiring reconciliation into the
  commit gate — deferred to a later pass.

### 8. M-CC-01/02/05: PR creation, state confirmation, race-safe idempotency
- **Status**: Completed
- **Description**: PyGithub/gh-backed open/update draft PR with client-side mutex + re-check
- **Pattern Source**: PyGithub · gh
- **Key Elements**:
  - Primary path: PyGithub (draft=True on create, no shell)
  - Discovery: repo.get_pulls(state="open", head="owner:branch", base=base) - owner:branch prefix mandatory
  - If found: pr.edit(title=..., body=...); else: repo.create_pull(..., draft=True)
  - State confirmation via repo.get_pull(n) → .state/.draft/.merged/.head.sha
  - Race-safety: client-side per-branch mutual exclusion (filelock/Redis SET NX) + re-check after lock
  - gh CLI fallback: fixed-argv list with shell=False + --body-file
  - Never call merge (omit pr.merge() / gh pr merge)
  - Surface remote_branch_state + pr_state (M-CC-02)
  - NEW SURFACE, dormant by default
- **Implementation**:
  - **NEW** `ProjectCapability.OPEN_PULL_REQUEST` (requires `push_development_commit`).
  - **NEW** `pr.py` `PullRequestManager` + `open_pull_request` MCP tool (dormant by
    default; runtime capability-gated via `resolve`).
  - Uses the `gh` CLI with fixed argv (no shell): discovers an existing PR via
    `gh pr list --head <branch>` (owner:branch form), updates it via `gh pr edit`,
    or creates a draft via `gh pr create --draft`. Merge is never issued.
  - Confirms `remote_branch_state` (present/vanished + head SHA) and `pr_state`
    (number/url/state/action). Race-safe: discover-then-create/update; a gh
    failure during discovery is re-raised (never mistaken for "no PR"), and the
    matched PR's `headRefName` is cross-checked against the branch.
  - Re-stated evidence: the caller's `fingerprint` must match a used verification
    record for the exact (project, job, base_head).
  - Tool count 23 → 24 (guard tests updated).
- **Files Modified**:
  - Created: `src/local_dev_mcp/pr.py`, `tests/test_pr.py`
  - Updated: `src/local_dev_mcp/server.py`, `src/local_dev_mcp/models.py`,
    `src/local_dev_mcp/__init__.py`, `tests/test_server.py`,
    `tests/test_checkpoints.py`, `tests/test_persistence.py`

### 9. M-CC-11: Prompt-injection defense for repo-ingested content
- **Status**: Completed
- **Description**: Systematic injection defense on content the reader ingests
- **Pattern Source**: NeMo-Guardrails · llm-guard (fork)
- **Key Elements**:
  - Borrow spotlighting/data-trust-boundary technique (fence + delimit untrusted repo content)
  - NeMo-Guardrails' programmable rails or maintained llm-guard fork as implementation
  - Run as fixed-argv on reader output (not networked service)
  - Internal hardening; no new tool surface
  - Hardens reader.py
- **Implementation**:
  - **NEW** opt-in `READER_INJECTION_DEFENSE` setting (default off; no new tool).
  - `reader.py` `_fence_untrusted` wraps content returned by `read_markdown` and
    `read_range` in a delimited data-trust boundary
    (`<!-- BEGIN UNTRUSTED REPOSITORY CONTENT ... -->` ... `<!-- END ... -->`)
    and neutralizes directive-like lines (e.g. "Ignore all previous
    instructions", "You are ...", "system:") by prefixing `> [untrusted data]`
    so they read as data, not instructions. The same per-line neutralizer is
    applied to `outline` heading text and `search` excerpts when enabled.
  - Deterministic and lossless for ordinary prose; the raw SHA-256 metadata is
    unchanged (fencing applies only to the returned `content`).
  - Internal hardening; no new MCP tool, parameter, or permission.
- **Files Modified**:
  - Updated: `src/local_dev_mcp/reader.py`, `src/local_dev_mcp/config.py`
  - Updated: `tests/test_reader.py`, `tests/test_config.py`
  - Updated: `.env.example`, `README.md`

### 10. M-EV-05: Authority-behind-every-governance-instruction
- **Status**: Completed
- **Description**: Every governance-changing instruction carries authority reference
- **Pattern Source**: langfuse · OpenTelemetry
- **Key Elements**:
  - Span-attribution model: every event carries actor, authority source, scope, timestamp
  - Stamp governance-changing events (status/extension/pause/terminal) with authority_ref in jobs._record_event
  - Sidecar authority_records + source_classifications (canonical/historical/asserted/observed/tested/inferred/superseded)
  - Internal, no new tool
  - Composes with M-GV-01 once authorizations table exists
- **Implementation**:
  - **NEW** additive `authority_records` + `source_classifications` tables in
    `bridge.db` (`SCHEMA_VERSION` stays 1; existing databases upgrade in place).
    `source_classifications` is seeded idempotently with the fixed vocabulary
    (canonical/historical/asserted/observed/tested/inferred/superseded).
  - `jobs._record_event` now accepts an optional `authority` block; every
    governance-changing event (created / status / extension / terminal /
    worktree_prepared) is stamped with an `authority_id` embedded in the event's
    `detail_json`, and the full authority record (actor, authority_source, scope,
    classification, recorded_at) is persisted to `authority_records`.
  - Authority sources: `chatgpt_confirmation` (canonical) for user-authorized
    creation/resume/cancel; `parent_delegation` (inferred) for delegated child
    jobs; `budget_policy` (inferred) for extensions; `bridge_internal`
    (observed) for status/terminal/worktree transitions.
  - `get_development_job` snapshot surfaces `authority_records` (additive, no
    new tool) so an orchestrator can audit who/what authorized each instruction.
  - Internal; no new MCP tool, parameter, or permission. Tool count stays 24.
- **Files Modified**:
  - Updated: `src/local_dev_mcp/store.py`, `src/local_dev_mcp/jobs.py`
  - Created: `tests/test_authority.py`

### 11. M-CC-11: Supply-chain gate — severity-gated pre-commit reject
- **Status**: Completed
- **Description**: Dependency/supply-chain scanning to reject HIGH/CRITICAL vulnerabilities
- **Pattern Source**: syft · grype · pip-audit
- **Key Elements**:
  - Recommended combo: syft (SBOM gen) + grype (--fail-on=high --only-fixed) + pip-audit (Python-native cross-check)
  - Reject commit if either reports HIGH/CRITICAL
  - Drop: Dependabot, Snyk (violates offline+OSS-clean constraints)
  - osv-scanner viable but needs Python-side severity filter (grype's --fail-on cleaner)
  - Internal, fixed-argv; feeds M-CC-11 and evidence bundle (syft SBOM is M-EV-06 artifact)
- **Implementation**:
  - **NEW** opt-in `supply_chain_scans` on `ProjectPolicy` (a list of
    `SupplyChainCommand` fixed-argv scanners, e.g. `grype ... --fail-on=high`,
    `pip-audit ...`). Dormant unless a project opts in; no new tool.
  - `VerificationCommitManager.commit` runs every configured scanner inside the
    candidate worktree (isolated) with fixed argv (no shell) and a deterministic
    environment, **before** any staging. A non-zero exit (exactly how grype's
    `--fail-on` surfaces a HIGH/CRITICAL finding) rejects the commit; a scanner
    that cannot start also fails closed. The source checkout and candidate
    worktree are left unchanged on rejection.
  - **NEW** additive `supply_chain_scans` audit table in `bridge.db`
    (`SCHEMA_VERSION` stays 1; existing databases upgrade in place). One
    append-only ACID row per scan (scanner, argv, exit code, passed, timestamp)
    so a rejected commit leaves a durable audit trail.
  - Refactored the verification command runner onto a shared `_run_exec` helper
    (no behavior change to verification commands).
  - Internal; no new MCP tool, parameter, or permission. Tool count stays 24.
- **Files Modified**:
  - Updated: `src/local_dev_mcp/models.py`, `src/local_dev_mcp/verification.py`,
    `src/local_dev_mcp/store.py`
  - Created: `tests/test_supply_chain.py`

### 12. M-CC-11: Sandboxed execution of repo-controlled scripts
- **Status**: Completed
- **Description**: Run repo-controlled verification scripts under lightweight syscall sandbox
- **Pattern Source**: gVisor · nsjail · bubblewrap · Firecracker
- **Key Elements**:
  - For verification_commands: run under bwrap/nsjail/gVisor with --network=none --read-only + frozen clock
  - Project opt-in; fixed argv (bridge invokes sandbox runner, not sandbox script directly)
  - Network policy (none | allowlist) flows from M-CC-09
  - Internal; no new tool
- **Implementation**:
  - **NEW** opt-in `sandbox: bool` on `VerificationCommand` (default false; dormant
    unless a project opts in). No new tool.
  - `_run_command` wraps a sandboxed command in a fixed bubblewrap (bwrap) argv:
    `--unshare-net` (network none), `--unshare-ipc`, `--unshare-pid`,
    `--ro-bind / /` (read-only root), `--proc`/`--dev`/`--tmpfs /tmp`, a
    read-only bind of the candidate worktree (so a repo-controlled script cannot
    modify the candidate state it verifies), a frozen clock
    (`SOURCE_DATE_EPOCH=0`), and `--die-with-parent` (a killed bridge cannot
    orphan a sandboxed process). The bridge invokes the sandbox runner, never
    the sandbox script directly.
  - Opted-out commands are unchanged (no behavior change to existing projects).
  - Internal; no new MCP tool, parameter, or permission. Tool count stays 24.
- **Files Modified**:
  - Updated: `src/local_dev_mcp/models.py`, `src/local_dev_mcp/verification.py`
  - Created: `tests/test_sandbox.py`

## Implementation Principles to Follow

All implementations must adhere to BridgeLab's core principles:

1. **Additive-table + lazy-import + zero-surface discipline**
2. **Security boundaries** (from AGENTS.md):
   - No `bypassPermissions` or unrestricted Bash
   - No `shell=True`
   - Fixed argv
   - Reject hidden/protected/traversal/symlink paths
   - `.env`/tokens/certs/`.venv` never in archives or Git
   - Only push surface is opt-in `push_development_commit` (non-trunk, origin-only, no-force)
   - ChatGPT confirmation is outer authorization gate
   - Bridge never directly edits its production checkout
3. **NEW SURFACE features** must:
   - Be off by default
   - Require new ProjectCapability (with dependency capability)
   - Require ToolAnnotations-registered tool
4. **Merge/tag/release/deploy** stay prohibited unless explicitly authorized (operator merges)

## Next Steps for Continuation

To continue work in a separate session:
1. Review this progress document
2. Check the current git status and branch
3. Select the next highest-priority task from the pending list (typically #2: M-CT · MCPB-010 tail)
4. Follow the implementation pattern established for M-EV-01
5. Update task statuses and this document as work progresses

*Last updated: $(date)*