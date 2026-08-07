# MCP Tool Bridge — Agent Guide

This file is the repository entry point for ChatGPT/Codex sessions working on
the `mcp-chatgpt-plugin` project.

## Source of truth

Use this precedence order:

1. The user's explicit instructions in the current conversation.
2. The current and immediately preceding conversations in the
   `mcp-chatgpt-plugin` ChatGPT project. Those conversations are the source of
   truth for accepted recommendations, scope, and phase decisions.
3. The current pushed source on the authoritative GitHub branch.
4. Repository-local phase briefs and handoff documents.
5. Older milestone archives and general project documentation.

If conversation context is unavailable or compacted, use this file and the
linked handoff documents as its distilled continuation record. Do not invent a
missing decision. Inspect the authoritative branch and ask the user only when a
material ambiguity remains.

## Authoritative repository

- Repository: `https://github.com/anindyasundarbera/mcp-tool-bridge`
- Active development branch: `feature/updatingCapabilities`
- ChatGPT project/application: `mcp-chatgpt-plugin`

At the start of a phase, inspect the remote/current checkout and record the
actual branch and commit. A milestone artifact without `.git` metadata is not
authoritative over pushed source.

Do not commit, push, open a pull request, merge, tag, restart the live server,
or invoke a modifying live connector tool unless the user explicitly requests
that action.

## Product goal

Build a secure, multi-project MCP bridge through which ChatGPT can:

- discover exact allowlisted projects;
- read and exchange repository Markdown safely;
- delegate approved engineering work to Claude Code;
- isolate modifying jobs;
- verify changes deterministically; and
- commit only reviewed, verified, explicitly authorized paths.

The final deliverable after MCPB-006 is a reviewed source package and ZIP. Each
phase must remain independently testable and serve as the baseline for the next.

## Phase roadmap

### Canonical accepted sequence

This is the user-approved phase scope. Preserve the order and do not move an
item to another phase without a new explicit decision in the source-of-truth
conversation.

| Phase | Accepted scope |
|---|---|
| MCPB-001 — Multi-project foundation | Validated `projects.json`; `ProjectRegistry` and `ProjectPolicy`; `list_allowed_projects`; project-aware `send_to_local_dev`; per-project locks; duplicated effort and metadata-string fixes; invalid/disabled-project tests. |
| MCPB-002 — Repository Reader | List Markdown; search Markdown; read full Markdown; read line ranges; get Markdown outline; status and metadata. |
| MCPB-003 — Markdown Writer | Restricted relative paths; create/update modes; SHA-256 concurrency control; protected-path enforcement; structured diff response. |
| MCPB-004 — Isolated development | Per-job Git worktrees; clean-baseline checks; job status/cancellation; candidate handling for self-updates. |
| MCPB-005 — Verification and commit | Fixed verification commands; diff fingerprints; explicit staging paths; human-approved commits; no unrestricted Bash. |
| MCPB-006 — Durable state | SQLite-backed job and verification persistence; restart re-hydration; honest mid-flight reconciliation; no new tools or permissions. |
| MCPB-008 — Bounded publication | Opt-in `push_development_commit` capability and `push_detached_development_commit` tool: push exactly the verified detached commit to a new non-trunk remote branch on `origin`; no force/merge/tag/ref-move; one branch per job; fingerprint re-derived from the commit object. |

The detailed sections below refine how to implement and verify these accepted
items. They do not expand the authority of a phase. Supporting tests,
documentation, packaging, and security validation are required engineering
work, not additional product capabilities.

### MCPB-001 — Multi-project foundation (complete)

- Validated JSON project registry and exact `project_id` selection
- Capability-based server authorization
- `list_allowed_projects` and `get_project_status`
- Project-aware `send_to_local_dev`
- Per-project locks
- Project-specific model/effort policy
- `high` default; `ultracode` only for complex reasoning, substantial
  implementation, or explicit user override
- Self-managed bridge edits blocked pending isolated worktrees

### MCPB-002 — Repository Reader (complete)

- Project-aware Markdown list, literal search, full read, range read, outline,
  and metadata tools
- Bounded UTF-8 content and results
- SHA-256 and Git metadata
- Hidden, protected, traversal, non-Markdown, and symlink paths rejected
- Fixed `rg` argument vector without shell execution
- Milestone package version `0.2.0`; 23 tests passed in the prepared artifact

### MCPB-003 — Markdown Writer (complete)

Read these files completely before implementation:

- `docs/NEXT-SESSION-MCPB-003.md`
- `docs/MCPB-003-IMPLEMENTATION-BRIEF.md`

The accepted MCPB-003 source delivers a project-aware
`write_project_markdown` tool with:

- approved write-root enforcement;
- create/update modes;
- mandatory SHA-256 guard for updates;
- bounded UTF-8 content and unified diffs;
- safe write mechanics and exact content preservation;
- shared per-project modifying lock;
- structured before/after evidence;
- no deletion, commit, shell, Claude invocation, or directory creation.

The package version is `0.3.0`; 36 core tests passed. The accepted source was
pushed to `feature/updatingCapabilities` at
`9cee986b4b62db6fc26e974642754793f7f71776`, and the MCPB-003 tools were
confirmed through the live connector before MCPB-004 implementation.

### MCPB-004 — Isolated development (complete)

Read these files completely before review or further implementation:

- `docs/MCPB-004-IMPLEMENTATION-BRIEF.md`
- `docs/NEXT-SESSION-MCPB-004.md`

The accepted MCPB-004 source adds:

- per-job Git worktrees;
- clean-baseline checks;
- asynchronous job IDs, status, result, and cancellation;
- bridge-created session resume only;
- staged candidate workflow for the self-managed bridge;
- candidate server health check on a separate port;
- no autonomous production overwrite, restart, merge, or rollback switch.

The package version is `0.4.0`; 51 core tests passed. The accepted source was
pushed to `feature/updatingCapabilities` at
`ca63a64f207c8e59207995b467244a98a5b92d4d`. The live connector exposed the
MCPB-004 lifecycle tools, but the self-managed project was not yet enabled for
a modifying live job.

### MCPB-005 — Verification and commit (complete)

Read these files completely before review or further implementation:

- `docs/MCPB-005-IMPLEMENTATION-BRIEF.md`
- `docs/NEXT-SESSION-MCPB-005.md`

The prepared MCPB-005 source adds:

- deterministic, fixed/allowlisted verification commands;
- structured verification evidence and diff fingerprints;
- commit only an unchanged, previously verified state;
- exact authorized staging paths using `git add -- <paths>`, never `git add .`;
- expected-HEAD and allowed-path checks;
- separate human approval before commit;
- no unrestricted Bash, push, merge, tag, or branch publication.

The prepared package version is `0.5.0`. It has not been committed, pushed,
installed, restarted, or exercised through a modifying live connector. The
source checkout remains unchanged; verification and commits operate only on
retained detached job worktrees.

The intended closure flow is:

```text
develop in isolated worktree
  -> review diff
  -> verify fixed commands
  -> bind verification to HEAD + diff fingerprint
  -> approve commit
  -> stage exact paths and commit verified state
```

After MCPB-005, run the complete verification and archive audit, then prepare
the final ZIP requested by the user.

### MCPB-006 — Durable state (implemented locally; pending acceptance)

Read these files completely before review or further implementation:

- `docs/MCPB-006-IMPLEMENTATION-BRIEF.md`
- `docs/NEXT-SESSION-MCPB-006.md`

The prepared MCPB-006 source adds:

- a focused SQLite persistence layer (`src/local_dev_mcp/store.py`, stdlib
  `sqlite3`, no new dependency) under a private bridge-owned `.state` directory
  beneath `JOB_WORKTREE_ROOT`;
- synchronous re-hydration of job and verification records in each manager's
  `__init__`, before the event loop starts;
- one ACID persist per state transition, inside the existing per-manager locks;
- honest reconciliation: a non-terminal job at crash time is reloaded as
  `failed` (`bridge restarted while job was in progress`); a terminal job whose
  worktree directory has vanished is reloaded as `failed`
  (`job worktree no longer exists`);
- fail-closed loading (unparseable rows skipped; a newer-than-supported schema
  version refuses to start) so one corrupt record never blocks startup;
- no new MCP tool, tool parameter, schema, safety annotation, permission
  broadening, or worktree auto-pruning.

The prepared package version is `0.6.0`. It has not been committed, pushed,
installed, restarted, or exercised through a modifying live connector. The
source checkout remains unchanged; persistence is an internal side-effect of
existing mutations.

### MCPB-008 — Bounded detached-commit publication

Read these files completely before review or further implementation:

- `docs/MCPB-008.md`
- `docs/MCPB-008-IMPLEMENTATION-BRIEF.md`

The accepted MCPB-008 source adds:

- an opt-in `push_development_commit` capability (requires `verification_commit`)
  and a `push_detached_development_commit` MCP tool;
- publication of **exactly** the bridge-created detached commit to a
  brand-new `refs/heads/<branch_name>` on the `origin` remote, via the refspec
  `<commit_sha>:refs/heads/<branch>` with no force flag;
- rejection of the repository default branch, `main`, and `master`; a required
  absent remote branch; and never force-push, merge, tag, delete, or move
  another ref;
- confirmation that the commit belongs to the retained job
  (`committed_sha == commit_sha`) and realizes the verified candidate
  fingerprint (re-derived directly from the commit object via
  `committed_state`, plus a used verification record for the exact evidence);
- one branch per job, with idempotent retry to the same branch and recovery if
  the remote ref vanished; a second distinct branch is rejected;
- an additive `publications` table in `bridge.db` (`SCHEMA_VERSION` stays 1)
  re-hydrated synchronously in `PublicationManager.__init__`;
- no trunk update, PR creation, deletion, worktree cleanup, or configurable
  remote (`origin` only).

The prepared package version is `0.8.0`; 127 core tests pass. The capability
is dormant unless a project opts in; `cairn-knowledge-store` is granted
`push_development_commit` in `projects.json`.

## Non-negotiable security boundaries

- OpenAI connector mTLS protects the public ngrok endpoint.
- Expose only the MCP server port; never expose Ollama directly.
- Tool callers provide stable project IDs and relative paths, never arbitrary
  filesystem roots.
- The local project registry is the authorization source of truth.
- Unknown, disabled, malformed, or unauthorized projects fail closed.
- Reject hidden paths, protected paths, traversal, and symlink escapes.
- Claude Code may use `acceptEdits` only within its fixed project boundary.
- Claude Bash, web, and secondary MCP tools remain disabled unless a later
  accepted design replaces them with deterministic allowlisted operations.
- Never enable `bypassPermissions` or unrestricted Bash.
- ChatGPT confirmation remains the outer authorization gate for modifying
  operations.
- No tool may silently commit, push, merge, tag, delete, restart production, or
  broaden its own permissions. The only push surface is the opt-in
  `push_development_commit` capability: an explicit, human-approved,
  non-silent push of one verified detached commit to a new non-trunk branch on
  `origin`, never a force-push, merge, tag, trunk update, or ref move.
- The running bridge must not directly edit its production checkout. Its own
  changes require the MCPB-004 candidate-worktree workflow.
- `.env`, tokens, certificates, credentials, virtual environments, caches, and
  local registry secrets must never enter milestone archives or Git.

## Engineering rules

- Preserve existing behavior unless the active phase explicitly changes it.
- Put reusable repository/path/write logic in focused modules rather than
  growing a monolithic server file.
- Enforce authorization and validation in server code; do not rely on model
  instructions for security.
- Use explicit Pydantic/FastMCP schemas and accurate tool safety annotations.
- Never invoke subprocesses with `shell=True`.
- Use fixed argument vectors for every allowed command.
- Do not expose absolute project roots, secrets, or raw environment data in
  tool responses.
- Bound inputs, outputs, files, searches, diffs, execution time, and model
  turns.
- Treat Git helper errors as errors; do not silently ignore return codes or
  stderr.
- Keep modifying operations serialized per project while allowing independent
  projects to proceed separately.
- Preserve unrelated user changes in dirty worktrees. Stop if the phase cannot
  be implemented without overwriting them.

## Verification requirements for every phase

Before declaring a phase complete:

1. Run the full prior + current core test suite with no network/model
   dependency.
2. Test failure paths and prove rejected operations leave repositories
   unchanged.
3. Inspect FastMCP tool discovery, parameter schemas, defaults/enums, and safety
   annotations.
4. Build and install the package in an isolated environment.
5. Compile/import the production entry points.
6. Audit the source/archive for secrets, `.env`, certificates, `.venv`, caches,
   and stale build output.
7. Report exact test counts and any checks that can run only on the user's live
   machine.
8. Record changed files and the ZIP SHA-256 when a milestone archive is made.
9. Update README, `.env.example`, package version, phase design, and the next
   session handoff.

Live ngrok, Ollama, Claude, and real-repository smoke tests are separate from
core tests and must be identified as such. Do not fabricate their results.

## Session start checklist

1. Read this `AGENTS.md` completely.
2. Read the current phase handoff and implementation brief completely.
3. Inspect Git status, branch, recent commits, project files, and existing
   tests.
4. Compare pushed source with the handoff; pushed source wins for code state,
   while accepted conversation decisions win for intended behavior.
5. Run the baseline tests before editing.
6. Implement only the active phase.

## Current resume pointer

Resume by reviewing and accepting **MCPB-008** using:

```text
docs/MCPB-008.md
docs/MCPB-008-IMPLEMENTATION-BRIEF.md
```

MCPB-007 (independent candidate review) is accepted and committed; MCPB-008
adds the bounded publication capability on top of it. After acceptance,
explicitly authorize any desired commit/push, live installation/restart, or
real-repository smoke test as separate actions.
