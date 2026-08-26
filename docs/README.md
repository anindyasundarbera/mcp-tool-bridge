# docs/

Documentation for the Local Development MCP bridge (BridgeLab). The repo
root `README.md` is the front door; this directory is the deeper reference and
the milestone record.

---

## Reference (start here)

| Document | What it covers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, request flow, governed lifecycle, durable state, isolation/locking, module map, threat model |
| [TOOLS.md](TOOLS.md) | Full reference for all 32 MCP tools — params, capability gates, annotations, what each refuses |
| [CAPABILITIES.md](CAPABILITIES.md) | The 15 capabilities, their dependency graph, the `projects.json` schema, opt-in discipline |
| [GETTING-STARTED.md](GETTING-STARTED.md) | Install, register a project, run, run a full governed job end-to-end, full limits table |
| [SECURITY.md](SECURITY.md) | Non-negotiable security boundaries, the two mutation surfaces, the threat model, hardening checklist |

---

## Milestone decision records

Each shipped milestone has a decision record (`MCPB-0NN.md`) describing what was
accepted, why, and the security implications; most also have an implementation
brief (`MCPB-0NN-IMPLEMENTATION-BRIEF.md`). These are historical and
authoritative for intent; **pushed source is authoritative for current code
state**.

| Milestone | Decision record | Brief | Scope |
|---|---|---|---|
| MCPB-001 | [MCPB-001.md](MCPB-001.md) | — | Multi-project foundation: validated registry, `ProjectPolicy`, `list_allowed_projects`, per-project locks |
| MCPB-002 | [MCPB-002.md](MCPB-002.md) | — | Repository reader: list/search/read/range/outline/metadata |
| MCPB-003 | [MCPB-003.md](MCPB-003.md) | [brief](MCPB-003-IMPLEMENTATION-BRIEF.md) | Markdown writer: SHA-256-guarded create/update beneath approved roots |
| MCPB-004 | [MCPB-004.md](MCPB-004.md) | [brief](MCPB-004-IMPLEMENTATION-BRIEF.md) | Isolated development: per-job worktrees, clean-baseline checks, job lifecycle |
| MCPB-005 | [MCPB-005.md](MCPB-005.md) | [brief](MCPB-005-IMPLEMENTATION-BRIEF.md) | Verification and commit: fixed commands, diff fingerprints, exact-path staging, human-approved detached commit |
| MCPB-006 | [MCPB-006.md](MCPB-006.md) | [brief](MCPB-006-IMPLEMENTATION-BRIEF.md) | Durable state: SQLite persistence, restart re-hydration, honest reconciliation, zero new tools |
| MCPB-007 | [MCPB-007.md](MCPB-007.md) | [brief](MCPB-007-IMPLEMENTATION-BRIEF.md) | Candidate review: list/read candidate, record verdict, independent reviewer |
| MCPB-008 | [MCPB-008.md](MCPB-008.md) | [brief](MCPB-008-IMPLEMENTATION-BRIEF.md) | Bounded publication: opt-in push of one verified detached commit to a new non-trunk branch |
| MCPB-009 | [MCPB-009.md](MCPB-009.md) | [brief](MCPB-009-IMPLEMENTATION-BRIEF.md) | Canonical-checkout synchronization: ff-only to `origin/<default>` + pre-work update check |
| MCPB-010 | [MCPB-010.md](MCPB-010.md) | [brief](MCPB-010-IMPLEMENTATION-BRIEF.md) | Continuity release: turn-level mission state, paused/resumable states, budgets, checkpoints |
| MCPB-011 | [MCPB-011.md](MCPB-011.md) | — | Governed host runtime verification: one policy-named check, no shell, immutability recheck |
| MCPB-012 | [MCPB-012.md](MCPB-012.md) | — | Working-tree candidate capture: capture approved dirty-tree changes without touching the source |
| M-AL-01 | [M-AL-01.md](M-AL-01.md) | — | Read-only introspection: `list_development_jobs`, `get_effective_project_policy`, `resolve_candidate_canonicalization` |

---

## Roadmap and planning

| Document | What it covers |
|---|---|
| [MCPB-ROADMAP.md](MCPB-ROADMAP.md) | The capability roadmap: audits all 149 capability items against the real codebase (DONE/PARTIAL/MISSING) and turns each unclosed gap into an implementable milestone |
| [MCPB-CAPABILITY-ELEVATION.html](MCPB-CAPABILITY-ELEVATION.html) | Open-source scan report: 11 capability elevations in priority order + 3 plan-changing findings |
| [MIGRATION_FROM_V1.md](MIGRATION_FROM_V1.md) | Migrating from the v1 single-project bridge to the multi-project v2 registry |

---

## Archived (not bridge documentation)

| Document | What it covers |
|---|---|
| [exploratory-rag/](exploratory-rag/README.md) | Archived notes from a separate RAG-augmentation effort (LightRAG / RAG-Anything / tokengate). Relocated from the repo root; not bridge docs. |

---

## How to read this directory

- **New to the project?** Read the root `README.md`, then
  [ARCHITECTURE.md](ARCHITECTURE.md), then [GETTING-STARTED.md](GETTING-STARTED.md).
- **Authorizing a new project?** [CAPABILITIES.md](CAPABILITIES.md) and the
  worked example in `projects.example.json`.
- **Wondering what a tool accepts/refuses?** [TOOLS.md](TOOLS.md).
- **Reviewing a security boundary?** [SECURITY.md](SECURITY.md).
- **Implementing a milestone?** Read the relevant `MCPB-0NN.md` decision record
  + brief, and the non-negotiable rules in `AGENTS.md` and
  [SECURITY.md](SECURITY.md). The roadmap tracks what is DONE / PARTIAL /
  MISSING.
- **An AI agent working on this repo?** Start at `AGENTS.md` (repo root).