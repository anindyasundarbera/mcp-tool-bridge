# MCPB-009 — Canonical checkout synchronization (implementation brief)

## Objective

Add one bounded, opt-in capability so the guarded canonical checkout of an
allowed project can be fast-forwarded to the verified current tip of origin's
default branch after an upstream pull-request merge. The surface is dormant
unless a project enables `sync_canonical_checkout`.

Read alongside `docs/MCPB-009.md` (the decision record).

## Architecture

### `src/local_dev_mcp/models.py`

- New `ProjectCapability.SYNC_CANONICAL_CHECKOUT = "sync_canonical_checkout"`.
- In `ProjectPolicy.validate_policy`: `sync_canonical_checkout` requires
  `repository_read`. It is independent of the development-job pipeline; a
  project may have read + sync without `claude_edit`/`verification_commit`.

### `src/local_dev_mcp/store.py`

- Additive `syncs` audit table (no schema-version bump; `CREATE TABLE IF NOT
  EXISTS`):

  ```sql
  CREATE TABLE IF NOT EXISTS syncs (
      sync_id INTEGER PRIMARY KEY AUTOINCREMENT,
      project_id TEXT NOT NULL,
      synced_at TEXT NOT NULL,
      from_head TEXT NOT NULL,
      to_head TEXT NOT NULL,
      branch TEXT NOT NULL,
      remote_url TEXT NOT NULL,
      idempotent INTEGER NOT NULL
  );
  ```

- `load_sync_rows(conn) -> list[dict]` (ordered by `sync_id`), `insert_sync_row
  (conn, row)` (append-only, one ACID transaction). No single-use bound; the
  table is an append-only audit log keyed by an autoincrement `sync_id`.

### `src/local_dev_mcp/jobs.py`

- `async def has_active_jobs(project_id) -> bool`: under `_state_lock`, True if
  any job for the project is non-terminal. The per-project lock is held for a
  whole job run, so this catches jobs that are queued but have not yet acquired
  the project lock.
- `async def stale_resumable_job_summaries(project_id) -> list[dict]`: under
  `_state_lock`, returns `{job_id, base_head}` for jobs that are
  `COMPLETED`, `task is None`, `committed_sha is None`, `worktree is not None`
  — i.e. still able to verify/commit, and whose ability ends once the canonical
  HEAD moves. Returns only `job_id` + `base_head`; no worktree path or session.

### `src/local_dev_mcp/sync.py` (new)

- `SyncError(RuntimeError)`.
- `@dataclass(frozen=True, slots=True) SyncRecord` + `sync_to_row` /
  `sync_from_row`.
- `SyncManager(settings, locks, jobs, worktrees=None, projects=())`: opens the
  same `bridge.db`, creates its own `GitWorktreeManager` for
  `ensure_state_root()` (mirrors `PublicationManager`), re-hydrates the
  append-only `syncs` log synchronously in `__init__`, appends one ACID row per
  sync inside `self._syncs_lock`.
- `audit(project_id=None) -> list[dict]`: read-only accessor over the
  re-hydrated log.
- `async def sync(project, expected_current_head, target_commit) -> dict`:

  1. Lowercase the hex evidence.
  2. Per-project lock.
  3. `has_active_jobs(project.id)` → `SyncError` if true (non-terminal job).
  4. Read canonical state `branch --show-current`, `rev-parse HEAD`,
     `status --porcelain=v1 --untracked-files=all`.
  5. Dirty checkout → `SyncError` (never discard changes).
  6. `head != expected_current_head` → `SyncError` (re-check status).
  7. `_remote_default_branch(project.root)`; None → `SyncError` ("cannot
     determine origin default branch").
  8. `branch != default_branch` → `SyncError` (must be on the default branch;
     reports `detached HEAD` when `branch` is empty).
  9. `remote get-url origin` (else "origin remote is not configured").
  10. **Idempotent fast path**: if `head == target_commit`, compute warnings,
      persist a row with `idempotent=1`, return `already_synchronized`. No
      fetch, no merge, no remote-tip guard (origin may have advanced further;
      the checkout is already at the requested commit).
  11. **Moving path**: `git fetch origin`; `remote_tip = rev-parse --verify
      refs/remotes/origin/<default>`; `remote_tip != target_commit` →
      `SyncError` ("not the current tip"). `git merge --ff-only
      <target_commit>`; any `GitCommandError` → `SyncError` ("fast-forward
      failed … diverged").
  12. Post-verify: `rev-parse HEAD == target_commit`, status clean,
      `branch == default_branch`; any mismatch → `SyncError`.
  13. Compute warnings via `stale_resumable_job_summaries`, keeping only those
      whose `base_head != target_commit`.
  14. Persist a row with `idempotent=0`; return `_redact`-ed response over the
      full path set.

  Reuses `run_git` (`git.py`), `_remote_default_branch` and `_redact`
  (`verification.py`). No `shell=True`; fixed argv throughout.

### `src/local_dev_mcp/server.py`

- Construct `SyncManager` (DI parameter `sync`, sharing
  `settings/locks/jobs/registry.projects()`).
- Register `sync_canonical_checkout` with params `project_id`,
  `expected_current_head` (`^[0-9a-f]{40}$`), `target_commit`
  (`^[0-9a-f]{40}$`), all required, and `ToolAnnotations(readOnlyHint=False,
  destructiveHint=True, idempotentHint=True, openWorldHint=True)` (openWorld:
  network fetch). Body resolves `SYNC_CANONICAL_CHECKOUT` then calls
  `active_sync.sync`.
- Instructions extended: the canonical checkout may be synchronized only to
  the verified current tip of origin's default branch, by fast-forward only,
  when clean and on the default branch; sync never pushes, forces, merges,
  tags, or moves a remote ref.

### `projects.json` / `projects.example.json`

- `cairn-knowledge-store.capabilities` gains `sync_canonical_checkout`.

## Concurrency

`sync` holds the per-project lock for its whole body, serializing
synchronization against every other modifying operation for that project
(writer, jobs, verification, review, publication). The `syncs` list is guarded
by a separate `_syncs_lock` acquired only for list appends. The per-project
lock is held for an entire job run, so a running job blocks sync at the lock;
the active-job check then refuses queued jobs. Lock ordering is
`project → syncs` (one direction); no deadlock.

## Security analysis

The threat model is "an attacker (or a confused operator) moves the guarded
canonical checkout to an arbitrary commit, discards local work, or breaks
in-flight jobs silently." Each is blocked:

- **Arbitrary commit.** After `git fetch origin`, `target_commit` must equal
  `refs/remotes/origin/<default>` — the verified current remote tip. A
  caller-supplied SHA that is not the remote tip is refused before any merge.
- **Discarded history.** `git merge --ff-only` cannot create a merge commit or
  force; a divergent local default branch fails closed, preserving local
  commits.
- **Discarded working-tree changes.** A dirty checkout (tracked or untracked)
  is refused before any git mutation.
- **Wrong branch.** Detached HEAD and non-default branches are refused; only
  the default branch advances.
- **Silent job breakage.** A non-terminal job blocks sync; completed-
  uncommitted jobs that will lose verify/commit are returned as explicit
  warnings.
- **Race on current head.** `expected_current_head` must equal the canonical
  HEAD; a stale view fails closed.
- **Path leakage.** All git output is `_redact`-ed over the full path set; the
  response carries only the branch, `from_head`, `to_head`, and remote URL.

## Trust boundary (documented, not enforced)

Synchronization updates the guarded canonical checkout to already-merged
upstream commits. It is **not** the bridge authoring code in its production
checkout (still forbidden; goes through candidate worktrees). It does not
push, open a PR, merge, tag, or update a remote trunk. `origin`-only keeps it
to the configured canonical remote. The operator remains the outer
authorization gate.

## Risks and limitations (documented, not auto-fixed)

- **Network and credentials.** `git fetch origin` runs in the bridge process's
  environment; fetch credentials must be available there. A fetch failure or
  timeout surfaces as a `SyncError`; the checkout is unchanged (fetch happens
  only on the moving path, before any merge).
- **Default-branch detection.** Requires `refs/remotes/origin/HEAD` to be set
  (normal for cloned repos; `git remote add` alone does not set it). If unset,
  sync fails closed rather than guessing the branch.
- **`origin` only.** A project whose canonical remote is not `origin` cannot
  sync until a remote named `origin` is configured. A configurable sync remote
  is intentionally excluded.
- **Completed-uncommitted jobs.** Sync warns but does not block on them; the
  operator is responsible for committing or abandoning such candidates, whose
  verify/commit path ends once the base moves.

## Required tests

`tests/test_sync.py` (new) builds a real local repo on `main` with a **local
bare repo as `origin`** (with `refs/remotes/origin/HEAD` set), simulates an
upstream merge by cloning the bare, committing, and pushing, then exercises:

- happy path: canonical `main` fast-forwards to the verified remote tip;
  remote `main` untouched; clean checkout;
- idempotent already-synchronized (target == current head); no-op;
- audit row persisted and reloaded by a fresh `SyncManager`;
- rejects: dirty checkout (change preserved), wrong `expected_current_head`,
  detached HEAD, non-default branch, target not the remote tip, divergent
  local history (local commit preserved), missing `origin`;
- blocks while a non-terminal job exists (injected queued job);
- warns about a completed-uncommitted job that loses its verify/commit path
  (and no warning when the job's base matches the target);
- `has_active_jobs` / `stale_resumable_job_summaries` accessor tests;

Plus `SyncCapabilityRegistryTests`: `sync_canonical_checkout` requires
`repository_read`; a project without the capability cannot resolve it. Tool-
count assertions bumped 21 → 22 in `test_server.py` and `test_persistence.py`.

## Acceptance criteria

- `.venv/bin/python -m pytest -q` is green (143 tests: 127 prior + 16 new).
- `list_tools()` returns 22 tools including `sync_canonical_checkout` with
  params `project_id`, `expected_current_head`, `target_commit` (all required,
  `^[0-9a-f]{40}$` patterns) and `ToolAnnotations(readOnlyHint=False,
  destructiveHint=True, idempotentHint=True, openWorldHint=True)`.
- `list_allowed_projects` advertises `sync_canonical_checkout` for
  `cairn-knowledge-store`.
- A project without `sync_canonical_checkout` cannot call the tool (capability
  gate fail-closed at `resolve`).
- Not committed, pushed, installed, restarted, or exercised against a live
  repository without separate explicit authorization.

## Explicitly excluded

- Force-push, merge-commit creation, tag, PR creation, remote trunk update,
  deletion, configurable remote (`origin` only).
- Synchronizing to a commit that is not the current `origin/<default>` tip, to
  a detached HEAD, to a non-default branch, or to a dirty checkout.
- Discarding local history (non-fast-forward) or working-tree changes.
- A schema-version bump; `syncs` is additive.
- Any new surface for projects that do not opt in.