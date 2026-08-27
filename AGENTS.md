# AGENTS.md — agent guide for the MCP Tool Bridge

This file is the repository entry point for AI agents (ChatGPT/Codex, Claude
Code, or any other) working on the `mcp-tool-bridge` (BridgeLab) project.
Read it completely before editing anything.

The deep reference docs live in [`docs/`](docs/README.md):
[ARCHITECTURE.md](docs/ARCHITECTURE.md),
[TOOLS.md](docs/TOOLS.md),
[CAPABILITIES.md](docs/CAPABILITIES.md),
[GETTING-STARTED.md](docs/GETTING-STARTED.md),
[SECURITY.md](docs/SECURITY.md).

---

## 1. Source of truth

Use this precedence order:

1. The user's explicit instructions in the current conversation.
2. The current and immediately preceding conversations in the
   `mcp-chatgpt-plugin` ChatGPT project — the source of truth for accepted
   recommendations, scope, and phase decisions.
3. The current pushed source on the authoritative GitHub branch.
4. Repository-local phase briefs and handoff documents in `docs/`.
5. Older milestone archives and general documentation.

If conversation context is unavailable or compacted, use this file and the
linked handoff documents as its distilled continuation record. Do not invent a
missing decision. Inspect the authoritative branch and ask the user only when
a material ambiguity remains.

## 2. Authoritative repository

- Repository: `https://github.com/anindyasundarbera/mcp-tool-bridge`
- ChatGPT project/application: `mcp-chatgpt-plugin`
- The active development branch is whatever the user is working on; at the
  start of a phase, inspect the remote/current checkout and record the actual
  branch and commit. A milestone artifact without `.git` metadata is not
  authoritative over pushed source.

## 3. Commit after every capability addition (mandatory workflow)

**Any change made after a capability addition must be committed.** This is a
standing rule, not a request you wait for.

A "capability addition" is any of:
- a new `ProjectCapability` value in `models.py`;
- a new MCP tool registered in `server.py`;
- a new opt-in surface, schema table, or `ToolAnnotations` change;
- a change to the capability dependency rules in `ProjectPolicy.validate_policy`;
- any supporting work that ships with the above (models, managers, store
  tables, tests, docs).

When you land such a change, do **all** of the following before declaring it
done, then commit:

1. **Bump the package version** in `pyproject.toml` (and `__init__.py` if kept
   in sync) per the milestone versioning convention.
2. **Bump the tool-count canaries** — the exact tool surface is asserted in
   `tests/test_server.py`, `tests/test_checkpoints.py`, and
   `tests/test_persistence.py`. Update every assertion that counts tools.
3. **Run the full core test suite** with no network/model dependency:
   `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v`. It must
   pass before the commit.
4. **Record a decision** in `docs/MCPB-0NN.md` (and an implementation brief
   `docs/MCPB-0NN-IMPLEMENTATION-BRIEF.md` when the milestone warrants one).
   Update `docs/MCPB-ROADMAP.md` and the "Current resume pointer" below.
5. **Update the docs** touched by the change — `README.md`, `docs/TOOLS.md`,
   `docs/CAPABILITIES.md`, and the tool-count in any doc that states it.
6. **Commit** on the current working branch with a conventional message
   (`feat(mcpb-0NN): ...`, `fix: ...`, `docs: ...`). End the message with:
   ```
   Co-Authored-By: Claude <noreply@anthropic.com>
   ```

**Committing is the one mutation that does not require separate user
authorization in this repo.** Pushing, opening a pull request, merging, tagging,
restarting the live server, or invoking a modifying live connector tool *do*
require explicit user authorization each time — see §6.

Rationale: a capability addition that is left uncommitted creates silent drift
between pushed source and local state, and the bridge's own self-governance
loses its durable provenance trail. Committing keeps the working tree a
faithful record of what was actually built. If you are unsure whether a
change counts as a capability addition, commit anyway — it is the safe
default.

## 4. Product goal

Build a secure, multi-project MCP bridge through which ChatGPT can:

- discover exact allowlisted projects;
- read and exchange repository Markdown safely;
- delegate approved engineering work to Claude Code;
- isolate modifying jobs in detached worktrees;
- verify changes deterministically with fixed command vectors;
- commit only reviewed, verified, explicitly authorized paths;
- publish one verified detached commit to a new non-trunk branch (opt-in);
- synchronize the canonical checkout to `origin/<default>` after a merge
  (opt-in, fast-forward only).

Each phase must remain independently testable and serve as the baseline for
the next.

## 5. Phase roadmap

The accepted sequence. Preserve the order; do not move an item to another
phase without a new explicit decision in the source-of-truth conversation.
Decision records and briefs live in `docs/` (see [`docs/README.md`](docs/README.md)
for the full index).

| Phase | Accepted scope | Record |
|---|---|---|
| MCPB-001 | Multi-project foundation: validated `projects.json`, `ProjectRegistry`/`ProjectPolicy`, `list_allowed_projects`, per-project locks | `docs/MCPB-001.md` |
| MCPB-002 | Repository reader: list/search/read/range/outline/metadata | `docs/MCPB-002.md` |
| MCPB-003 | Markdown writer: SHA-256-guarded create/update beneath approved write roots | `docs/MCPB-003.md` |
| MCPB-004 | Isolated development: per-job worktrees, clean-baseline checks, job lifecycle | `docs/MCPB-004.md` |
| MCPB-005 | Verification and commit: fixed commands, diff fingerprints, exact-path staging, human-approved detached commit | `docs/MCPB-005.md` |
| MCPB-006 | Durable state: SQLite persistence, restart re-hydration, honest reconciliation, zero new tools | `docs/MCPB-006.md` |
| MCPB-007 | Candidate review: list/read candidate, record verdict, independent reviewer | `docs/MCPB-007.md` |
| MCPB-008 | Bounded publication: opt-in push of one verified detached commit to a new non-trunk branch | `docs/MCPB-008.md` |
| MCPB-009 | Canonical-checkout synchronization: ff-only to `origin/<default>` + pre-work update check | `docs/MCPB-009.md` |
| MCPB-010 | Continuity release: turn-level mission state, paused/resumable states, budgets, checkpoints, zero new tools | `docs/MCPB-010.md` |
| MCPB-011 | Governed host runtime verification: one policy-named check, no shell, immutability recheck | `docs/MCPB-011.md` |
| MCPB-012 | Working-tree candidate capture: capture approved dirty-tree changes without touching the source | `docs/MCPB-012.md` |
| M-AL-01 | Read-only introspection: `list_development_jobs`, `get_effective_project_policy`, `resolve_candidate_canonicalization` | `docs/M-AL-01.md` |

The detailed sections of each decision record refine how to implement and
verify the accepted items. They do not expand the authority of a phase.
Supporting tests, documentation, packaging, and security validation are
required engineering work, not additional product capabilities. The full
gap-closure plan and DONE/PARTIAL/MISSING audit live in
[`docs/MCPB-ROADMAP.md`](docs/MCPB-ROADMAP.md).

## 6. Non-negotiable security boundaries

These apply to every milestone. The full list with reasoning is in
[`docs/SECURITY.md`](docs/SECURITY.md); the critical subset:

- OpenAI connector mTLS protects the public ngrok endpoint. Expose only the
  MCP server port; never expose the configured Claude launcher directly.
- The local project registry (`projects.json`) is the only authorization
  source of truth. Unknown, disabled, malformed, or unauthorized projects fail
  closed. Enforce authorization and validation in server code, never in model
  instructions.
- Tool callers provide stable project IDs and relative paths — never
  filesystem roots, raw session IDs, executables, argv, environment, cwd, or
  timeouts.
- Reject hidden, protected, traversal, and symlink-escape paths.
- Claude Code may use `acceptEdits` only within its fixed project worktree.
  Bash, web, secondary MCP tools, slash commands, and `bypassPermissions`
  remain disabled. Never enable `bypassPermissions` or unrestricted Bash.
- Never invoke subprocesses with `shell=True`. Use fixed argument vectors for
  every allowed command.
- No tool may silently commit, push, merge, tag, delete, restart production, or
  broaden its own permissions. The only push surface is opt-in
  `push_development_commit` (non-trunk, origin-only, no-force). The only
  local-checkout update surface is opt-in `sync_canonical_checkout`
  (fast-forward only to `origin/<default>`).
- The opt-in `runtime_verification` capability is evidence-only: it runs one
  policy-named check and never grants commit authority. The caller supplies
  only `project_id`, `check_id`, `expected_head`.
- The opt-in `working_tree_candidate_capture` capability is read-only against
  the source checkout; the scope is pinned in trusted policy.
- The running bridge must not directly edit its production checkout — its own
  changes require the candidate-worktree workflow.
- `.env`, tokens, certificates, credentials, `.venv`, caches, and local
  registry secrets must never enter Git or milestone archives.

## 7. Engineering rules

- Preserve existing behavior unless the active phase explicitly changes it.
- Put reusable repository/path/write logic in focused modules rather than
  growing a monolithic server file. Keep the one-way dependency direction:
  `server` → managers → `store` → `worktrees`; `store` imports only `worktrees`.
- Keep durable-state schema changes **additive**: `CREATE TABLE IF NOT
  EXISTS`, `SCHEMA_VERSION` stays 1, no `ALTER TABLE` on existing tables
  without a sidecar table; idempotent `ALTER TABLE ADD COLUMN` guarded by
  `PRAGMA table_info` is allowed for additive columns.
- Use explicit Pydantic/FastMCP schemas and accurate `ToolAnnotations`.
- Never expose absolute project roots, secrets, or raw environment data in
  tool responses.
- Bound inputs, outputs, files, searches, diffs, execution time, and model
  turns.
- Treat Git helper errors as errors; do not silently ignore return codes or
  stderr.
- Keep modifying operations serialized per project while allowing independent
  projects to proceed separately.
- Preserve unrelated user changes in dirty worktrees. Stop if the phase
  cannot be implemented without overwriting them.

## 8. Verification requirements for every phase

Before declaring a phase complete:

1. Run the full prior + current core test suite with no network/model
   dependency (`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`).
2. Test failure paths and prove rejected operations leave repositories
   unchanged.
3. Inspect FastMCP tool discovery, parameter schemas, defaults/enums, and
   safety annotations.
4. Build and install the package in an isolated environment (`pip install -e .`).
5. Compile/import the production entry points.
6. Audit the source/archive for secrets, `.env`, certificates, `.venv`,
   caches, and stale build output.
7. Report exact test counts and any checks that can run only on the user's
   live machine.
8. Bump the tool-count canaries and the package version; commit (per §3).
9. Update `README.md`, the relevant `docs/`, `.env.example`, and the next
   session handoff.

Live ngrok, launcher, Claude, and real-repository smoke tests are separate from
core tests and must be identified as such. Do not fabricate their results.

## 9. Session start checklist

1. Read this `AGENTS.md` completely.
2. Read the current phase handoff / decision record in `docs/` completely.
3. Inspect Git status, branch, recent commits, project files, and existing
   tests. Run `git fetch --prune origin` before any base-branch reasoning
   (local `main` may be stale).
4. Compare pushed source with the handoff; pushed source wins for code state,
   while accepted conversation decisions win for intended behavior.
5. Run the baseline tests before editing:
   `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v`.
6. Implement only the active phase.

## 10. Current resume pointer

**All roadmap capability milestones R1–R3 and Cross-cutting are DONE** as of
the 2026-08-27 integration: MCPB-001→012, M-AL-01, M-CT-02…07, M-EV-01…08,
M-GV-01…07, M-CC-01…11 — 22 capability commits cherry-picked onto `main` in
dependency order; the full suite is green (**572 tests**). See
[`docs/MCPB-ROADMAP.md`](docs/MCPB-ROADMAP.md) for the authoritative status.
**M-IN-01 (P3) remains a non-goal.** No capability gaps are open.

Next work if requested: release/versioning (HEAD is pinned at `0.10.4`
through the cherry-picks) or any follow-on hardening; branch off `main` and
follow §3.

When you land any capability change from this point on, follow §3 and commit
it; explicitly authorize any desired push, PR, live installation/restart, or
real-repository smoke test as separate actions.