# MCPB-008 — Bounded detached-commit publication (implementation brief)

## Objective

Add one bounded, opt-in publication capability so a reviewed and verified
detached development commit can be published to a brand-new remote branch on
`origin`, with no force-push, merge, tag, trunk update, or ref movement. The
surface is dormant unless a project enables `push_development_commit`.

Read alongside `docs/MCPB-008.md` (the decision record).

## Architecture

### `src/local_dev_mcp/models.py`

- New `ProjectCapability.PUSH_DEVELOPMENT_COMMIT = "push_development_commit"`.
- In `ProjectPolicy.validate_policy`: `push_development_commit` requires
  `verification_commit` (which transitively requires `claude_edit`,
  `commit_roots`, and `verification_commands`). No other field is added.

### `src/local_dev_mcp/store.py`

- Additive `publications` table (no schema-version bump; `CREATE TABLE IF NOT
  EXISTS` upgrades existing databases in place):

  ```sql
  CREATE TABLE IF NOT EXISTS publications (
      project_id TEXT NOT NULL,
      job_id TEXT NOT NULL,
      commit_sha TEXT NOT NULL,
      branch_name TEXT NOT NULL,
      remote_url TEXT NOT NULL,
      published_at TEXT NOT NULL,
      PRIMARY KEY (project_id, job_id)
  );
  ```

- `upsert_publication_row`, `load_publication_rows`,
  `publication_for(conn, project_id, job_id) -> dict | None`.
- `used_verification_record(conn, project_id, job_id, base_head,
  diff_fingerprint) -> dict | None` reads the existing `verifications` table
  where `used = 1` and the evidence matches exactly — the authorization proof.

### `src/local_dev_mcp/jobs.py`

- `@dataclass(frozen=True, slots=True) CommittedCandidate: committed_sha,
  worktree`.
- `async def committed_candidate(project, job_id) -> CommittedCandidate`:
  under `_state_lock`, require `status == COMPLETED`, `task is None`,
  `committed_sha is not None`, `worktree is not None`. Parallels
  `verification_worktree` but for the post-commit state; never exposes the raw
  session ID.

### `src/local_dev_mcp/verification.py`

- Public `async def committed_state(settings, project, worktree, commit_sha,
  base_head, paths) -> CandidateState`: re-derives the fingerprint from the
  `commit_sha` object relative to `base_head` over `paths`. Mirrors
  `_staged_state` exactly except the new entry comes from
  `git ls-tree -z <commit_sha> -- <path>` (instead of `ls-files --stage`) and a
  missing entry means deletion. Reuses `_FINGERPRINT_MARKER`, `_frame`,
  `_validate_policy_path`, `_git_bytes`, `_digest_git_blob`, and the
  `100644`/`100755`/symlink/submodule guards.
- `PublicationError(RuntimeError)`.
- `_validate_branch_name(name)`: deterministic refname + trunk validation (no
  subprocess); rejects full refs, `HEAD`, slashes, leading dots, `..`,
  `.lock`, `@{`/`@`, control/forbidden characters, and `main`/`master`/`HEAD`.
- `_remote_branch_sha(root, branch_name, *, timeout_seconds)`: `ls-remote
  --heads origin <branch>` → SHA or None.
- `_remote_default_branch(root, *, timeout_seconds)`: best-effort
  `symbolic-ref refs/remotes/origin/HEAD` (full form, prefix-stripped; `--short`
  yields `origin/<name>` which is wrong), or None.
- `_paths_from_verification_row(row)`: parses `paths_json` defensively.
- `@dataclass PublicationRecord` + `publication_to_row` / `publication_from_row`.
- `PublicationManager(settings, locks, jobs, worktrees=None, projects=())`:
  opens the same `bridge.db`, re-hydrates publications synchronously in
  `__init__`, persists each mutation as one ACID transaction inside
  `self._publications_lock`.
- `async def publish(project, job_id, commit_sha, branch_name,
  expected_base_head, expected_diff_fingerprint) -> dict`:

  1. Lowercase the hex evidence; `_validate_branch_name(branch_name)`.
  2. Per-project lock.
  3. `committed_candidate` → `commit_sha == committed_sha` and
     `expected_base_head == worktree.base_head`, else `PublicationError`.
  4. `used_verification_record(...)` must exist; `paths` from its row.
  5. Resolve the commit: `rev-parse --verify <sha>^{commit}`; parent
     `rev-parse <sha>^` must equal `expected_base_head`; `diff-tree` paths
     must equal `paths`.
  6. `committed_state(...)` fingerprint must equal
     `expected_diff_fingerprint`.
  7. `remote get-url origin` (else "origin remote is not configured").
  8. Reject `branch_name == default_branch`.
  9. Idempotency: if a `publications` row exists for `(project, job)`:
     - different branch/commit → rejected;
     - same → `ls-remote`; if `== commit_sha` return idempotent success; if
       non-None and different → rejected; if None (vanished) → re-push and
       return success.
  10. No prior row: pre-check the remote branch is absent (else rejected);
      push `<commit_sha>:refs/heads/<branch_name>` with
      `-c core.hooksPath=/dev/null`; post-verify `ls-remote == commit_sha`;
      persist the `PublicationRecord`; return.
  - Lower-level `(DevelopmentJobError, GitCommandError, VerificationError,
    WorktreeError)` are converted to `PublicationError`; explicit
    `PublicationError`s propagate as-is. The response is `_redact`-ed over the
    full path set.

### `src/local_dev_mcp/server.py`

- Construct `PublicationManager` (DI parameter `publication`, sharing
  `settings/locks/jobs/registry.projects()`).
- Register `push_detached_development_commit` with params `project_id`,
  `job_id` (`^[0-9a-f]{32}$`), `commit_sha` (`^[0-9a-f]{40}$`), `branch_name`
  (`min_length=1, max_length=200`), `expected_base_head` (`^[0-9a-f]{40}$`),
  `expected_diff_fingerprint` (`^[0-9a-f]{64}$`), and
  `ToolAnnotations(readOnlyHint=False, destructiveHint=True,
  idempotentHint=True, openWorldHint=True)`. Body resolves
  `PUSH_DEVELOPMENT_COMMIT` then calls `active_publication.publish`.
- Instructions extended: a detached commit may be published only to a new
  non-trunk branch; never force-push, merge, tag, move another ref, or update a
  trunk.

### `projects.json`

- `cairn-knowledge-store.capabilities` gains `push_development_commit`.

## Concurrency

`publish` holds the per-project lock for its whole body, serializing
publication against every other modifying operation for that project (writer,
jobs, verification, review). The `publications` dict is guarded by a separate
`_publications_lock` acquired only for dict reads/writes. The verifications
table is read-only here (WAL serializes the readers with the verification
writer). Lock ordering is `project → publications` (one direction); there is
no reverse ordering and no deadlock.

## Security analysis

The threat model for publication is "an attacker convinces the bridge to push
the wrong content, push to a trunk, overwrite a ref, or push twice to fan
out." Each is blocked:

- **Wrong content.** `commit_sha` must equal the bridge-set `committed_sha`;
  the fingerprint is recomputed from the commit object and must equal the
  verified fingerprint; the parent and path set are checked. A caller cannot
  supply an arbitrary object — it must be the exact object the bridge created
  from the verified staged state.
- **Trunk push.** `main`/`master`/`HEAD` and the detected default branch are
  rejected before any network operation. `branch_name` is a short, validated
  refname; the refspec is `refs/heads/<branch>`.
- **Ref overwrite.** No force flag; the remote branch must not exist (pre- and
  post-checked). Only the one refspec is pushed.
- **Fan-out.** One `publications` row per `(project, job)`; a second distinct
  branch is rejected.
- **Path leakage.** All git output is `_redact`-ed over the full path set; the
  response carries only the branch name, commit SHA, and remote URL.

The re-derivation (`committed_state`) is defense-in-depth: even if the jobs
table's `committed_sha` were somehow wrong, the recomputed fingerprint would
not match `expected_diff_fingerprint`, and the push would be refused.

## Trust boundary (documented, not enforced)

Publication forwards an already-reviewed, already-verified, already-committed
object. It does not create the commit, open a PR, merge, or update a trunk.
`origin`-only keeps publication to the configured canonical remote. The
operator remains the outer authorization gate (separate human approval before
`commit_verified_development_job`, and again before this push).

## Risks and limitations (documented, not auto-fixed)

- **Network and credentials.** `git push` runs in the bridge process's
  environment; push credentials must be available there (git credential
  helper, SSH keys, etc.). The bridge does not supply them. A push timeout or
  auth failure surfaces as a `PublicationError`; nothing is left half-pushed
  (the remote branch either exists at `commit_sha` or not at all).
- **Concurrent remote mutation.** Between the `ls-remote` pre-check and the
  push, another actor could create the remote branch; the non-force push then
  fails (non-fast-forward), which is converted to a `PublicationError`. The
  post-push `ls-remote` verifies the ref points at exactly `commit_sha`.
- **Default-branch detection.** `refs/remotes/origin/HEAD` is best-effort;
  when unset (common for locally-added remotes), only `main`/`master` are
  rejected by name. `main` and `master` are rejected unconditionally
  regardless.
- **`origin` only.** A project whose canonical remote is not `origin` cannot
  publish until a remote named `origin` is configured. A configurable
  `push_remote` is intentionally excluded.

## Required tests

`tests/test_publication.py` (new) builds a real local repo with a **local bare
repo as `origin`**, runs a job → verify → commit, then exercises:

- happy path: remote branch created at exactly `commit_sha`; `main` (remote
  and local) unchanged; parent is the verified base;
- idempotent retry of the same branch; second distinct branch rejected (and
  never created);
- `main`/`master`/`MAIN` and a configured default branch (`develop` via
  `remote set-head`) rejected;
- pre-existing remote branch rejected; wrong `commit_sha`; wrong fingerprint;
  wrong base HEAD;
- bad branch names (full refs, `HEAD`, dots, `..`, `.lock`, `@`, `@{`, space,
  over-length, control chars, consecutive/leading/trailing slashes);
- missing `origin` rejected;
- recovery when the remote branch vanished after a prior publish (re-push).

Plus: tool-count assertions bumped 20 → 21 in `test_server.py` and
`test_persistence.py`; new-tool schema assertions in `test_server.py`;
`publications` row round-trip + `used_verification_record` exact-match tests in
`test_persistence.py`; capability-gate tests in `test_registry.py` and
`test_config.py`.

## Acceptance criteria

- `PYTHONPATH=src python -m unittest discover -s tests` is green (127 tests:
  115 prior + 12 new).
- `list_tools()` returns 21 tools including `push_detached_development_commit`.
- `list_allowed_projects` advertises `push_development_commit` for
  `cairn-knowledge-store`.
- A project without `push_development_commit` cannot call the tool (capability
  gate fail-closed).

## Explicitly excluded

- Force-push, merge, tag, PR creation, trunk update, deletion, worktree
  cleanup, configurable remote.
- Publishing to an existing remote branch, the default branch, `main`, or
  `master`.
- Publishing a commit not created by `commit_verified_development_job` for the
  specified retained job, or not realizing the verified fingerprint.
- A schema-version bump; `publications` is additive.
- Any new surface for projects that do not opt in.