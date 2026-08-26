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
| MCPB-009 — Canonical checkout synchronization | Opt-in `sync_canonical_checkout` capability and tool: fast-forward the guarded canonical checkout to exactly the verified current tip of `origin/<default>`; clean + default-branch + exact-expected-head required; `git merge --ff-only`; no push/force/merge-commit/tag/ref-move; additive `syncs` audit table; refuses while a non-terminal job exists, warns about completed-uncommitted jobs that lose verify/commit. |
| MCPB-010 — Continuity release | Internal turn-level mission state: durable `job_checkpoints`/`job_events`/`job_progress` tables (additive, `SCHEMA_VERSION` stays 1); a `paused_*` family of `JobStatus` (resumable) plus `failed_infrastructure`/`quarantined_provenance` (terminal, non-resumable); crash-with-checkpoints → `paused_interrupted` instead of `failed`; soft/multi-dimensional/progress-gated budgets (`CLAUDE_SOFT_TURNS`/`CLAUDE_EXTENSION_TURNS`/`CLAUDE_MAX_EXTENSIONS`/`CLAUDE_HARD_TURNS`/`CLAUDE_MAX_NO_PROGRESS_TURNS`/`CLAUDE_MAX_TOOL_FAILURES`); in-place `--resume <session_id>` extension slices; checkpoint compaction; opt-in `CLAUDE_STREAM_JSON` turn counting; structured terminal report in `get_development_job`. Zero new MCP tools, parameters, or permissions. |
| MCPB-011 — Governed host runtime verification | Opt-in `runtime_verification` capability (requires `repository_read`) and `run_runtime_verification` tool: execute one policy-named runtime check against the clean canonical checkout at an exact expected HEAD, no shell (`asyncio.create_subprocess_exec`), caller supplies only `project_id`/`check_id`/`expected_head` (never executable/argv/env/cwd/timeout); clean + exact-HEAD + default-branch binding, read-only git only; repository immutability rechecked post-execution (mutation → FAIL, no auto-restore); content-addressed evidence with `rtv_`-prefixed IDs that cannot authorize a commit; bounded/redacted output; additive `runtime_verifications` audit table. Evidence-only: no commit/push/merge/tag/ref-move. |

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

### MCPB-009 — Canonical checkout synchronization (implemented locally; pending acceptance)

Read these files completely before review or further implementation:

- `docs/MCPB-009.md`
- `docs/MCPB-009-IMPLEMENTATION-BRIEF.md`

The prepared MCPB-009 source adds:

- an opt-in `sync_canonical_checkout` capability (requires `repository_read`)
  and a `sync_canonical_checkout` MCP tool;
- fast-forward of the guarded canonical checkout to **exactly** the current
  tip of `origin`'s default branch (`git fetch origin` → require
  `target_commit == refs/remotes/origin/<default>`) via `git merge --ff-only
  <target_commit>`, with no force flag and no merge commit;
- a required clean checkout (tracked and untracked), a required default-branch
  checkout (detached HEAD and other branches refused), and a required
  `expected_current_head` that must equal the canonical HEAD;
- refusal while any non-terminal development job exists, and warnings about
  completed-but-uncommitted jobs that lose their verify/commit path after the
  base moves (sync still proceeds);
- an additive `syncs` audit table in `bridge.db` (`SCHEMA_VERSION` stays 1),
  re-hydrated synchronously in `SyncManager.__init__`, one append-only ACID
  row per synchronization;
- no push, merge-commit creation, tag, deletion, remote ref move, or
  configurable remote (`origin` only).

MCPB-009 also adds a pre-work update companion so the orchestrator never starts
new work on a stale base:

- a `prepare_canonical_for_work` MCP tool (same capability as
  `sync_canonical_checkout`) that fetches `origin` and reports whether the
  canonical checkout is `up_to_date`, `updates_available`, `ahead`, `diverged`,
  `dirty`, `no_origin`, or `fetch_failed` — **without merging**. With
  `pull=true` it fast-forwards to the auto-discovered verified remote tip
  (reusing the `sync` ff-only guards; no SHA arguments required);
- a stale-base guard in worktree creation: when no `requested_base` is pinned
  and the canonical checkout is behind `origin/<default>`, `send_to_local_dev`
  refuses with an actionable message naming `prepare_canonical_for_work(pull=true)`
  rather than silently branching new work off a stale `base_head`. The guard
  reads **local remote-tracking refs only** (no `git fetch` on the worktree-creation
  hot path), so it is armed by the `prepare_canonical_for_work` fetch above; if no
  one has fetched it no-ops rather than blocking on the network. It is skipped
  when there is no `origin` (offline / local-only repos) and when `requested_base`
  is pinned. A `diverged` checkout still requires a human steward — fast-forward
  cannot reconcile it.

**Orchestrator pre-work convention:** after a pull request has been raised (and
especially after one has merged on the remote) and before `send_to_local_dev` for
the next piece of work, call `prepare_canonical_for_work(pull=false)`. If the
verdict is `updates_available`, call `prepare_canonical_for_work(pull=true)` to
fast-forward the canonical checkout to the verified remote tip, then proceed.
This keeps the tree clean through a bridge tool instead of throwing an opaque
stale-base error that a human steward must fix with a manual `git pull`.

The prepared package version is `0.9.0`; 143 core tests pass. The capability
is dormant unless a project opts in; `cairn-knowledge-store` is granted
`sync_canonical_checkout` in `projects.json`. It has not been committed,
pushed, installed, restarted, or exercised against a live repository.

### MCPB-010 — Continuity release (implemented locally; pending acceptance)

Read these files completely before review or further implementation:

- `docs/MCPB-010.md`
- `docs/MCPB-010-IMPLEMENTATION-BRIEF.md`

The prepared MCPB-010 source adds **internal** turn-level mission state — zero
new MCP tools, parameters, or permissions (mirroring MCPB-006's
`test_durable_state_adds_no_new_mcp_tools`):

- three additive tables in `bridge.db` — `job_checkpoints`, `job_events`
  (append-only audit ledger), and a sidecar `job_progress` table — created with
  `CREATE TABLE IF NOT EXISTS`; `SCHEMA_VERSION` stays 1, no `ALTER TABLE` on
  the existing `jobs` table, existing databases upgrade in place;
- a `paused_*` family of `JobStatus` — `paused_budget_exhausted`,
  `paused_interrupted`, `blocked_input_required` (resumable) and
  `failed_infrastructure`, `quarantined_provenance` (terminal, non-resumable) —
  so budget exhaustion and a stalled run pause rather than fail; genuine
  candidate failures stay `failed`;
- crash reconciliation that preserves progress: a non-terminal job **with**
  checkpoint rows reloads as `paused_interrupted` (resumable from the last
  checkpoint); **without** checkpoints it reloads as `failed` (unchanged);
- soft, multi-dimensional, progress-gated budgets via `CLAUDE_SOFT_TURNS`,
  `CLAUDE_EXTENSION_TURNS`, `CLAUDE_MAX_EXTENSIONS`, `CLAUDE_HARD_TURNS`,
  `CLAUDE_MAX_NO_PROGRESS_TURNS`, `CLAUDE_MAX_TOOL_FAILURES`; progress is a
  changed-path delta reusing `verification.candidate_changed_paths` +
  `partial_fingerprint`; a progressing run gets an in-place `--resume
  <session_id>` extension slice, a stalled run pauses, a tool-failure storm
  terminates as `failed_infrastructure`;
- `resume_development_job` accepts `paused_*` jobs, keeps the cumulative
  budget, and seeds the drafter from the last checkpoint's changed-path set;
  non-resumable states are rejected;
- checkpoint compaction (`compact_checkpoints` keeps all
  boundary/extension/milestone rows + the most recent N sample rows), triggered
  at terminal transitions and above `CLAUDE_CHECKPOINT_MAX`;
- opt-in `CLAUDE_STREAM_JSON` drives Claude with `--output-format stream-json
  --verbose` and a pure `_parse_output` that derives real `num_turns`/
  `tool_failures`/`usage`; the default path stays `json` with the sampler
  driving progress (turn counts are then sample counts); the reviewer never
  streams;
- a structured terminal report in `get_development_job` (`_snapshot`): always
  present `prohibited_actions`, full `budget` object, `checkpoint_count`,
  `milestones`, `blockers`, `pause_reason`, and the latest checkpoint's
  `candidate_fingerprint`/`changed_path_count`/`changed_paths` — additive, no
  new tool.

The prepared package version is `0.10.0`; 167 core tests pass (143 prior + 11
new `test_claude` + 13 new `test_checkpoints`). It has not been committed,
pushed, installed, restarted, or exercised against a live repository.

### MCPB-011 — Governed host runtime verification (implemented locally; pending acceptance)

Read this file completely before review or further implementation:

- `docs/MCPB-011.md`

The prepared MCPB-011 source adds one opt-in capability and one MCP tool for
running checks that need host-runtime access (Docker daemon, loopback
networking, host-managed services) against the canonical checkout:

- a new `runtime_verification` capability (requires `repository_read`, like
  `sync_canonical_checkout` and `export_evidence_bundle`), dormant by default;
- a `RuntimeVerificationCommand` policy model with a fixed `argv` (validated,
  shell executables rejected, `{python}` only as `argv[0]`), a small validated
  `environment` (interpreter/loader injection keys forbidden, `PATH` allowed),
  `execution_mode: "host"`, and an optional bounded `timeout_seconds`;
- one tool `run_runtime_verification(project_id, check_id, expected_head)` whose
  schema exposes **only** those three parameters — the caller can never supply
  an executable, argv, env, cwd, timeout, or shell text; `check_id` is a
  pattern-anchored lookup key that is never executed;
- execution via `asyncio.create_subprocess_exec` (no `shell=True`/`bash -c`),
  `start_new_session=True`, in a new process group (timeout kills the group
  `SIGTERM`→`SIGKILL`), with a minimal deterministic environment plus policy
  overrides (the bridge's full process env is **not** inherited — no
  token/cert leakage);
- canonical binding with **read-only git only** (never fetch/pull/merge/
  checkout/switch/stash/clean): clean working tree, `HEAD == expected_head`,
  and on the default branch where BridgeLab knows it; binding failures fail
  closed before any subprocess is spawned;
- repository immutability rechecked after execution — any HEAD/working-tree/
  branch drift makes the run `status="failed"` with `repository.mutated=true`,
  no auto-restore, no PASS;
- content-addressed evidence: `rtv_`-prefixed IDs (a separate namespace from the
  candidate `verification_id` pattern, so a runtime ID can never authorize a
  commit), a `policy_fingerprint` and a deterministic `evidence_fingerprint`,
  bounded/redacted stdout/stderr with hashes over the redacted text (not a
  secret-confirmation oracle), and `_redact` plus long-policy-env-value
  redaction; the absolute project root never appears;
- one additive `runtime_verifications` audit table in `bridge.db` (both PASS
  and bounded FAIL rows persisted, re-hydrated on restart); `SCHEMA_VERSION`
  stays 1, existing databases upgrade in place;
- evidence-only: a successful runtime check never stages, commits, pushes,
  merges, tags, opens a PR, moves a ref, modifies policy, or enables a
  capability.

The prepared source adds 26 new `test_runtime_verification` tests and bumps
the tool-count canaries in `test_server`/`test_checkpoints`/`test_persistence`
from 28 to 29; 312 tests pass. It has not been committed, pushed, installed,
restarted, or exercised against a live Docker host (the unit suite uses
self-contained `{python}` scripts so it runs without Docker).

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
- The opt-in `runtime_verification` capability is evidence-only: it runs one
  policy-named check against the canonical checkout and never grants commit
  authority. The MCP caller may supply only `project_id`, `check_id`, and
  `expected_head` — never an executable, argv, environment, working directory,
  timeout, or shell text; there is no `run_command`/`exec`/`shell`/`bash`
  surface. Commands execute directly with `asyncio.create_subprocess_exec`
  (no shell) and the bridge's full process environment is not inherited.
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

Resume by reviewing and accepting **M-AL-01** (read-only introspection) using:

```text
docs/M-AL-01.md
```

M-AL-01 adds three dormant-by-default, capability-gated read-only introspection
tools (MCP tool count 24 -> 27): `list_development_jobs`,
`get_effective_project_policy`, and `resolve_candidate_canonicalization`, plus
the `job_introspection`/`policy_introspection`/`canonical_history_read`
capabilities (each gated on its prerequisite). It is the agent-lab Phase-0
enabler, named M-AL-01 (not MCPB-011, which the roadmap reserves for the
Evidence release M-EV-01). Implemented and verified locally (262 tests green,
including an adversarial multi-agent review that found and fixed four
INDETERMINATE-vs-NOT_CANONICAL correctness defects);
**NOT COMMITTED, NOT PUSHED, NO PR OPENED, LIVE BRIDGELAB NOT RESTARTED.** The
prior milestone, MCPB-010 (internal turn-level continuity state, zero new MCP
tools), is implemented locally and pending acceptance. After acceptance of
either, explicitly authorize any desired commit/push, live
installation/restart, or real-repository smoke test as separate actions.
