# Changelog

All notable changes to the BridgeLab MCP bridge (`local-dev-mcp`) are recorded
here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.11.0] - 2026-08-28

The full R1–R3 + cross-cutting roadmap lands on `main` (22 cherry-picked
capability commits) and the three former roadmap PARTIAL rows are closed.
Tool surface: **32 → 39 tools**; capabilities: **15 → 22**.

### Added

- **`replay_verification` read-only tool (NEW SURFACE)** — deterministic
  replay of a recorded verification against a read-only detached checkout at
  the recorded `base_head`, re-running the recorded commands through the same
  hermetic runner and recomputing environment + per-command digests. Surfaces
  command-definition changes as digest mismatches honestly; never silently
  re-runs a changed command. Gated by the new `replay_verification`
  capability (requires `verification_commit`; dormant by default).
- **Test-kind coverage** — `VerificationCommand.kind`
  (`unit | integration | property | mutation | ad_hoc`, default `unit` keeps
  every existing project unchanged). Property commands run under a
  deterministic seed derived from `base_head` + `command.id` (repetition
  suffixed), so property runs are reproducible; mutation output parses
  best-effort into advisory `mutation_stats`.
- **Repo static analysis / schema validation (internal, no new tool)** —
  optional `ProjectPolicy.static_analysis` block: declared analyzers (ruff /
  mypy / check-jsonschema …) run read-only over the candidate via the hermetic
  runner with structured findings; stdlib-only schema checks
  (`json` / `yaml`-as-TOML / `xml`) over changed files; `fail_on="error"`
  (opt-in) flips the verification conclusion to FAIL, `"never"` (default)
  keeps findings advisory. Results persist in the new
  `verification_static_analysis` sidecar (SCHEMA_VERSION stays 1).
- **R1 Continuity** — resumable checkpoints with rich detail sidecar,
  multi-dimensional budgets (`CLAUDE_HARD_WALL_SECONDS`), atomic milestone
  execution, reachable `QUARANTINED_PROVENANCE`, structured terminal report,
  progress-intelligence enrichment, canonical-sync + base-drift detection.
- **R2 Evidence** — requirement traceability (`RequirementManager`), structured
  verification result schema + environment/toolchain fingerprint,
  evidence-binding sidecars, content-addressed reviews + revision→finding
  links, source classifications + authority records, portable `export_evidence_bundle`
  tool, nondeterminism labeling, artifact retention + GC.
- **R3 Governance** — durable authorization objects + `authorizations` table,
  named roles (Planner / Evidence-officer / Supervisor, read-only), external
  apex grounding + ratified-spec runtime gating, self-certification
  prevention, sub-agent delegation with capability-subset enforcement,
  parallel verify + independent review with conflict reconciliation.
- **Cross-cutting M-CC** — `open_pull_request` (discover-then-create/update,
  race-safe idempotency), merge-vs-ratification separation, unexpected-files
  detection, read-only completed-worktree recovery, **human control surface**
  (6 dormant capability-gated control tools: `pause_development_job`,
  `extend_budget`, `narrow_scope`, `replace_worker`, `discard_candidate`,
  `revoke_authorization`), hermetic verification lab (`PIN_TOOLCHAIN` /
  `DEPS_CACHE_DIR` / `NETWORK_POLICY` / bwrap / deterministic env), failure
  recovery hardening, security hardening (filesystem/credential policies,
  signed manifests, sensitivity classification, supply-chain scans).

### Changed

- Tool surface **38 → 39** (the `replay_verification` read-only tool); the six
  M-CC-08 control tools ship dormant (opt-in via their capabilities).
- Capabilities **15 → 22** (six control capabilities + `replay_verification`).
- `VerificationCommand` gains an additive `kind` field (default `unit`).
- `verify_development_job` responses carry `kind` per command and, for
  mutation commands, advisory `mutation_stats`; `verify()` responses carry
  `static_analysis` + `static_analysis_summary` when a project opts in.

### Fixed

- No functional fixes shipped in this release; the PARTIAL-closure tail
  removed the roadmap's three remaining gaps (deterministic replay, test-kind
  coverage, static analysis/schema validation).

### Security

- All new surface is capability-gated and dormant by default
  (`replay_verification` requires `verification_commit`).
- Static-analysis and schema-check results are advisory unless a project opts
  into `fail_on="error"`.
- `.env`/tokens/certs are never included in evidence bundles or this release's
  artifacts.
