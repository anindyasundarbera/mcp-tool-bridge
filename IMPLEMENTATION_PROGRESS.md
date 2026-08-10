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
- **Status**: Pending
- **Description**: First open-source adoption adding runtime dependencies
- **Pattern Source**: Nix · devbox · nextest · Bazel · libfaketime
- **Key Elements**:
  - PIN_TOOLCHAIN via Nix flakes or devbox (bit-for-bit reproducible envs)
  - Flaky detection via deterministic repetitions (nextest's retry-classification model)
  - Deterministic env: TZ=UTC, SOURCE_DATE_EPOCH, PYTHONHASHSEED=0, sorted fs iteration, optional libfaketime
  - Baseline comparison against last green verification's fingerprint
  - Project opt-in; internal, fixed-argv
  - Future: replay_verification tool (NEW SURFACE)

### 4. M-CC-11 · M-EV-06: Evidence bundle + supply-chain attestation
- **Status**: Pending
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

### 5. M-GV-06: Sub-agent delegation + parent/child task trees
- **Status**: Pending
- **Description**: Bounded parent/child task trees with delegation mandates + records
- **Pattern Source**: LangGraph subgraphs · crewAI roles · openai-agents handoffs
- **Key Elements**:
  - Add parent_job_id + delegation_mandate_json to DevelopmentJob
  - Bound children via config.MAX_DELEGATIONS_PER_JOB
  - Record delegation in authorizations table (delegated_from)
  - Internal-first; delegate tool is NEW SURFACE (defer)

### 6. M-GV-01/02/03/04: Durable authorization + named roles + external grounding + spec-gating
- **Status**: Pending
- **Description**: Replace inline-evidence re-statement with durable authorizations table
- **Pattern Source**: OPA/Cedar · crewAI · spec-kit · MCP ToolAnnotations
- **Key Elements**:
  - Durable authorizations table (actor/authority_source/capability/target/issued_at/expires_at/single_use/delegated_from)
  - crewAI's role pattern: Planner/Evidence-officer/Supervisor as capability-gated invocations
  - spec-kit's spec-as-source-of-truth + ratified_spec_commit for external grounding and spec-gating
  - Internal-first; no new tool
  - Composes with M-EV-05 once authorizations table exists

### 7. M-GV-07: Parallel verify + review + conflict reconciliation
- **Status**: Pending
- **Description**: Parallel fan-out verification and review with conflict reconciliation
- **Pattern Source**: LangGraph fan-out/fan-in · Qodo multi-agent review
- **Key Elements**:
  - Once candidate stable, asyncio.gather verify + independent review concurrently
  - Qodo's multi-agent review model: separate review/improve/describe agents with PR-history indexing
  - Divergent verdicts trigger tie-breaker third reviewer with distinct reviewer_id
  - Commit gate requires reconciled pass
  - Additive reconcile_reviews(review_ids); no new tool

### 8. M-CC-01/02/05: PR creation, state confirmation, race-safe idempotency
- **Status**: Pending
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

### 9. M-CC-11: Prompt-injection defense for repo-ingested content
- **Status**: Pending
- **Description**: Systematic injection defense on content the reader ingests
- **Pattern Source**: NeMo-Guardrails · llm-guard (fork)
- **Key Elements**:
  - Borrow spotlighting/data-trust-boundary technique (fence + delimit untrusted repo content)
  - NeMo-Guardrails' programmable rails or maintained llm-guard fork as implementation
  - Run as fixed-argv on reader output (not networked service)
  - Internal hardening; no new tool surface
  - Hardens reader.py

### 10. M-EV-05: Authority-behind-every-governance-instruction
- **Status**: Pending
- **Description**: Every governance-changing instruction carries authority reference
- **Pattern Source**: langfuse · OpenTelemetry
- **Key Elements**:
  - Span-attribution model: every event carries actor, authority source, scope, timestamp
  - Stamp governance-changing events (status/extension/pause/terminal) with authority_ref in jobs._record_event
  - Sidecar authority_records + source_classifications (canonical/historical/asserted/observed/tested/inferred/superseded)
  - Internal, no new tool
  - Composes with M-GV-01 once authorizations table exists

### 11. M-CC-11: Supply-chain gate — severity-gated pre-commit reject
- **Status**: Pending
- **Description**: Dependency/supply-chain scanning to reject HIGH/CRITICAL vulnerabilities
- **Pattern Source**: syft · grype · pip-audit
- **Key Elements**:
  - Recommended combo: syft (SBOM gen) + grype (--fail-on=high --only-fixed) + pip-audit (Python-native cross-check)
  - Reject commit if either reports HIGH/CRITICAL
  - Drop: Dependabot, Snyk (violates offline+OSS-clean constraints)
  - osv-scanner viable but needs Python-side severity filter (grype's --fail-on cleaner)
  - Internal, fixed-argv; feeds M-CC-11 and evidence bundle (syft SBOM is M-EV-06 artifact)

### 12. M-CC-11: Sandboxed execution of repo-controlled scripts
- **Status**: Pending
- **Description**: Run repo-controlled verification scripts under lightweight syscall sandbox
- **Pattern Source**: gVisor · nsjail · bubblewrap · Firecracker
- **Key Elements**:
  - For verification_commands: run under bwrap/nsjail/gVisor with --network=none --read-only + frozen clock
  - Project opt-in; fixed argv (bridge invokes sandbox runner, not sandbox script directly)
  - Network policy (none | allowlist) flows from M-CC-09
  - Internal; no new tool

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