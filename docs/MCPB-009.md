# MCPB-009 — Canonical checkout synchronization

## Decision

The bridge keeps each project's **canonical (guarded) checkout** at
`project.root`. Development jobs never edit it; `GitWorktreeManager.create`
reads `project.root`'s HEAD as `base_head` and creates a detached worktree from
it, and `validate_resume` (used by `verify_development_job` and
`commit_verified_development_job`) requires the canonical HEAD to **still
equal** a job's `base_head`. When a pull request merges on remote `main`,
origin advances but the bridge's canonical checkout stays at the old commit,
so new jobs branch off a stale base and the operator receives:

> Remote main is verified at that exact commit, but BridgeLab's guarded
> checkout remains stale at a124096…. Please synchronize its canonical
> checkout to: 9f698f91…

Before MCPB-009 there was no tool to do this. MCPB-009 adds one bounded,
opt-in synchronization capability, **dormant by default** — a project that
does not opt in observes no new tool and no broadened permission:

`sync_canonical_checkout(project_id, expected_current_head, target_commit)`
fast-forwards one exact allowed project's guarded canonical checkout to
**exactly** `target_commit`, provided `target_commit` is the **current tip of
origin's default branch**. It:

- resolves the project with the `sync_canonical_checkout` capability (which
  requires `repository_read`);
- requires the canonical checkout to be **clean** (no tracked or untracked
  changes) and on the repository **default branch** (refuses detached HEAD and
  other branches);
- requires `expected_current_head` to equal the canonical HEAD (exact-evidence
  binding, like `expected_head`/`expected_base_head` elsewhere);
- runs `git fetch origin`, resolves origin's default branch via
  `refs/remotes/origin/HEAD`, and **requires `target_commit` to equal
  `refs/remotes/origin/<default>`** — the bridge never fast-forwards to an
  arbitrary caller-supplied commit, only to the verified remote default-branch
  tip;
- advances the local default branch with `git merge --ff-only <target_commit>`
  — no `--force`, no merge commit, fails closed on divergence so local history
  is never discarded;
- never pushes, merges (creates a merge commit), tags, deletes, or moves a
  remote ref;
- refuses while any **non-terminal** development job exists for the project,
  and warns about **completed-but-uncommitted** jobs that will lose their
  verify/commit path after the base moves (sync still proceeds).

It returns `status` (`synchronized` or `already_synchronized`), the branch,
`from_head`, `to_head`, the remote URL, an idempotent flag, and a warnings
list. An idempotent retry to a commit the checkout already sits at returns
`already_synchronized` without moving anything.

## Reconciliation

Synchronization is the read-side counterpart of MCPB-008 publication. It
reuses the per-project lock, the durable `bridge.db`, exact-evidence binding,
path redaction, and git runs through `run_git`. It adds **one** capability
(`sync_canonical_checkout`, which requires `repository_read`), **one** tool,
**one** additive `syncs` audit table, and **one** new manager
(`SyncManager`). `SCHEMA_VERSION` stays 1: the `syncs` table is created with
`CREATE TABLE IF NOT EXISTS`, so existing databases upgrade in place (per the
MCPB-006/007/008 additive philosophy). `SyncManager` re-hydrates the append-only
audit log synchronously in `__init__` and appends one ACID row per
synchronization. No existing tool, parameter, or safety annotation changes
for projects that do not opt in.

A pre-work update companion extends MCPB-009 without adding a capability:
`prepare_canonical_for_work(pull=false)` fetches `origin` and classifies the
canonical checkout against the remote default tip (`up_to_date`,
`updates_available`, `ahead`, `diverged`, `dirty`, `no_origin`, `fetch_failed`)
without merging; `prepare_canonical_for_work(pull=true)` fast-forwards to the
auto-discovered verified remote tip, reusing the `sync` ff-only guards with no
SHA arguments required. `SyncManager.sync` accepts optional
`expected_current_head`/`target_commit` (omitted on the auto-discovery path); the
precise `sync_canonical_checkout` tool is unchanged. A stale-base guard in
`worktrees.create` refuses to branch a non-pinned job off a canonical HEAD that
is behind `origin/<default>`, naming `prepare_canonical_for_work(pull=true)` so
the orchestrator self-serves the pull instead of hitting an opaque downstream
error; the guard is skipped when there is no `origin` or `requested_base` is
pinned, and a `diverged` checkout still requires a human steward.

The per-project lock is held for an entire job run (`DevelopmentJobManager._run`),
so sync naturally waits for any running job to finish before it acquires the
lock; the active-job check then refuses queued jobs that would otherwise
branch off a base about to move. `SyncManager` reuses `run_git` (`git.py`),
`_remote_default_branch` and `_redact` (`verification.py`), and the new job
accessors `DevelopmentJobManager.has_active_jobs` and
`stale_resumable_job_summaries` (`jobs.py`).

## Security

Every guard is enforced in server code, never in model instructions:

- **Only the verified remote tip.** After `git fetch origin`, the tool resolves
  `refs/remotes/origin/<default>` and requires `target_commit == remote_tip`.
  A caller cannot aim the bridge at an arbitrary commit; it can only move the
  canonical checkout to what `origin/<default>` points at *right now*.
- **Fast-forward only.** `git merge --ff-only <target_commit>` cannot create a
  merge commit or force anything; it fails if the local default branch has
  diverged, preserving local history. No `--force`, `--tags`, `--all`, or
  `--mirror`.
- **Clean and on the default branch.** A dirty checkout (tracked or untracked)
  is refused — working-tree changes are never discarded. A detached HEAD or a
  non-default branch is refused, so the tool only ever advances the branch the
  remote tracks.
- **Exact expected current head.** `expected_current_head` must equal the
  canonical HEAD, so a sync cannot race with an unnoticed checkout move; the
  operator re-checks `get_project_status` on mismatch.
- **No active job.** A non-terminal job blocks sync; the operator waits or
  cancels. Completed-uncommitted jobs that will lose verify/commit are surfaced
  as warnings, not silently broken.
- **No push, merge-commit, tag, delete, or remote ref move.** Sync touches
  only the local default branch and the working tree. Hooks are irrelevant
  (no commit is created). The `origin`-only constraint fails closed if origin
  is absent or its default branch cannot be resolved.
- **Output hygiene.** All git output is redacted with `_redact` over the full
  path set; only the branch, `from_head`, `to_head`, and remote URL are
  returned.

## Trust boundary (documented)

Synchronization is the operator's explicit, outer-most authorization to update
the guarded canonical checkout to already-merged upstream commits. It is
**not** the bridge authoring code in its production checkout — that remains
forbidden and still goes through the MCPB-004 candidate-worktree workflow. The
bridge does not push, open a pull request, merge, tag, or update a trunk in this
step; it only fast-forwards the local default branch to the verified remote
tip. The `origin`-only constraint keeps synchronization to the project's
configured canonical remote.

## Explicitly excluded

- Force-push, merge-commit creation, tag, PR creation, trunk update on the
  remote, deletion, or a configurable remote (`origin` only).
- Synchronizing to a commit that is not the current tip of `origin/<default>`,
  to a detached HEAD, to a non-default branch, or to a dirty checkout.
- Discarding local history (non-fast-forward) or local working-tree changes.
- A schema-version bump; `syncs` is additive (`SCHEMA_VERSION` stays 1).
- Any new surface for projects that do not opt in
  (`sync_canonical_checkout` is off by default and requires
  `repository_read`).