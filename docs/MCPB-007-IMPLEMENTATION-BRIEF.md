# MCPB-007 — Independent candidate review (bridge-spawned reviewer + review gate)

## Objective

Before MCPB-007, BridgeLab could run a candidate's verification commands and bind
a `(base_head, diff_fingerprint, paths)` triple, but it could not expose that
*retained* candidate to a *separate* reviewer. A reviewer could only see the
project's canonical checkout, and the semantic self-review was performed by the
drafting agent itself — the very agent whose work was being judged. There was no
bridge surface to read the candidate, no record of a review, and no way to make a
review a precondition of a commit.

MCPB-007 closes that gap with three opt-in layers, all dormant by default:

1. **Bounded candidate reads** — `list_candidate_changes` / `read_candidate_file`
   expose the retained worktree's changed paths to a reviewer, scoped to the
   candidate's changed-path set, read-only.
2. **Review records + an opt-in commit gate** — `record_candidate_review` /
   `get_candidate_review` persist a verdict bound to the candidate's evidence
   triple; `commit_verified_development_job` gains an optional `review_id`, and
   when `requires_review` is set a matching, still-unused review must be presented
   before a commit is allowed.
3. **A bridge-spawned, read-only reviewer** — `run_independent_review` launches a
   fresh, read-only, non-resumable Claude session distinct from the drafter.

The package version for this milestone is `0.7.0`.

MCPB-007 does not broaden any permission for a project that has not opted in, add
any dependency or persistence backend, bump the schema version, or change the
detached-commit-only / human-approval semantics of the commit path.

## Architecture

### `src/local_dev_mcp/models.py`

- New `ProjectCapability.CANDIDATE_REVIEW = "candidate_review"`.
- New `ProjectPolicy` booleans, both default `False`:
  `requires_review: bool = False`, `allow_bridge_reviewer: bool = False`.
  They are **not** advertised in `public_metadata` (they are internal policy,
  not a consumer-facing capability).
- `validate_policy` cross-rules:
  - `candidate_review` requires `claude_edit` (review only makes sense for a
    project the bridge can already edit in).
  - `requires_review` requires `verification_commit` (a review gate only binds
    a commit, and only the verification path produces committable evidence).
  - `allow_bridge_reviewer` requires `candidate_review` (the spawned reviewer
    reads the candidate through the same read surface).

### `src/local_dev_mcp/store.py`

An **additive** `reviews` table; `SCHEMA_VERSION` stays `1` (no bump — the table
is new but the schema seam is unchanged, and the version seam is fixed to handle
a future bump correctly):

```sql
CREATE TABLE IF NOT EXISTS reviews (
  review_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, job_id TEXT NOT NULL,
  base_head TEXT NOT NULL, diff_fingerprint TEXT NOT NULL, paths_json TEXT NOT NULL,
  reviewed_at TEXT NOT NULL, reviewer_id TEXT, verdict_json TEXT NOT NULL,
  used INTEGER NOT NULL);
```

- `load_review_rows(conn) -> list[dict]`, `upsert_review_row(conn, row)` (an
  `INSERT … ON CONFLICT DO UPDATE` inside `with conn:` — one ACID transaction),
  `review_id_exists(conn, review_id) -> bool`.
- **Version-seam fix** in `open_state_db`: if the stored `schema_meta.version` is
  *less than* `SCHEMA_VERSION`, `UPDATE` it forward and re-read (a no-op today,
  since `SCHEMA_VERSION` is still 1, but correct for a future additive bump); a
  stored version *newer* than supported still raises `StateStoreError` and fails
  closed. The previous code only handled `==` and `>`.
- `store` continues to import only `worktrees` — the dependency direction stays
  one-way (`review`/`verification`/`jobs` import `store`; `store` imports nothing
  from them), so no import cycle.

### `src/local_dev_mcp/verification.py`

Extracted a pure helper before `_candidate_state`:

- `candidate_changed_paths(settings, worktree) -> tuple[str, ...]` — discovers
  the candidate's changed paths (tracked diff `git diff --name-only --no-renames`
  + untracked `git ls-files --others --exclude-standard` via `_git_bytes`,
  `_decode_paths`, sort/dedupe), rejects an empty change set, and caps at
  `MAX_COMMIT_PATHS`. It does **no** index-empty check and **no** policy check —
  those stay in `_candidate_state`, which now calls
  `paths = await candidate_changed_paths(settings, worktree)` before its
  fingerprint loop. This is a behavior-preserving refactor: review and
  verification now share the single source of truth for the changed-path set, so
  a review can never bind or read a path set that disagrees with the verification
  evidence.
- `_open_candidate_read(root, relative, *, allow_missing) -> int | None` — the
  end-to-end-anchored, symlink-safe open used by every content read. `_safe_path`
  validates the directory chain with `O_DIRECTORY|O_NOFOLLOW` against a parent
  `dir_fd` but returns a *path string* and closes the anchored fds; callers used
  to re-resolve that string from the filesystem root with `os.open(abs, O_NOFOLLOW)`,
  which only protects the *final* component — a parent directory swapped for a
  symlink in the race window could redirect the read outside the worktree.
  `_open_candidate_read` instead opens the leaf with `O_NOFOLLOW` *relative to
  the already-validated parent descriptor* and returns that open fd, so the open
  is never re-resolved and no parent-symlink race can escape. A leaf that becomes
  a symlink after the `os.stat` check is refused by `O_NOFOLLOW` (ELOOP, not
  followed). Error filenames are the relative leaf, never the absolute worktree
  path. Returns `None` for a deletion when `allow_missing`; raises
  `VerificationError` on a symlink, non-regular, or missing path. `_safe_path` is
  retained only for `_python_paths` (which needs a path string for `PYTHONPATH`
  from operator-configured dirs, not changed paths) and the deletion-existence
  pre-check in `read_candidate_file`.

### `src/local_dev_mcp/claude.py`

- `build_reviewer_command(settings, project, effort, *, resume_session_id=None)`:
  builds the reviewer's command with **no** `--resume`, `--tools Read,Glob,Grep`,
  `--permission-mode default`, and **no** `acceptEdits` — the reviewer is a fresh,
  read-only session. It asserts `resume_session_id is None` (a reviewer must never
  resume the drafter's session).
- `ClaExecutor` refactor: extracted a shared bounded-subprocess core
  `_run_claude(command, project, effort, execution_root, text)` used by both
  `execute` (the drafter, via `build_claude_command` + optional resume) and the
  new `execute_review` (the reviewer, via `build_reviewer_command`, asserting
  `resume_session_id is None`). `execute_review` returns
  `status/result/is_error/exit_code/terminal_reason/errors/repository_before/after`
  — **no `session_id` key**: the reviewer is one-shot and never resumable. No
  subprocess harness is duplicated.

### `src/local_dev_mcp/review.py` (new — the safety-critical core)

- `ReviewError(RuntimeError)`; bounds: `MAX_REVIEW_VERDICT_CHARS = 20000`,
  `MAX_CANDIDATE_READ_BYTES = 1_048_576`; `_BRIDGE_REVIEWER_ID = "bridge-reviewer"`,
  reserved so only `run_independent_review` can mint it.
- `ReviewRecord` dataclass mirroring `VerificationRecord`:
  `review_id, project_id, job_id, base_head, diff_fingerprint, paths, reviewed_at,
  reviewer_id, verdict, used`; `review_to_row` / `review_from_row` for the
  store round-trip.
- Path/verdict hygiene: `_normalize_review_path` (rejects absolute, `..`,
  empty, non-canonical), `_validate_reviewer_id` (≤200 printable, and rejects the
  reserved `"bridge-reviewer"` so the caller path cannot forge an attested
  record), `_coerce_verdict` (must be a non-empty dict, JSON-serializable, ≤ the bound).
- Candidate read primitives reusing verification's discipline:
  `_candidate_file_metadata` (size/sha256/executable/deleted/exceeds-read-limit via
  the anchored `_open_candidate_read` fd and an `os.read` loop) and
  `_read_candidate_bytes` (a full bounded read through the anchored fd with a
  TOCTOU `fstat` re-check against the opened fd).
- `ReviewManager(settings, locks, jobs, executor, worktrees=None, projects=())`:
  opens `bridge.db` (its own connection; WAL supports multiple) and
  `_reload_reviews()` synchronously in `__init__`.
- `_reviewable_worktree_locked(project, job_id)` — assumes the project lock is
  already held; calls `jobs.verification_worktree` + `validate_resume` and
  converts `DevelopmentJobError`/`WorktreeError`/`GitCommandError` → `ReviewError`.
- `list_candidate_changes` (`async with project_lock`): re-derive
  `candidate_changed_paths`, `_validate_policy_path` per path, gather
  `_candidate_file_metadata`, redact. The whole path-processing body is wrapped
  `try/except VerificationError → ReviewError`.
- `read_candidate_file` (`async with project_lock`): normalize → re-derive
  changed-path set → membership check → `_validate_policy_path` → `_safe_path`
  (`allow_missing` for deletion reporting) → `_read_candidate_bytes` (or a
  deletion report) → sha256 check → optional line range → redact. Same
  `VerificationError → ReviewError` wrap.
- `record_candidate_review` (`async with project_lock` then `reviews_lock`):
  `_coerce_verdict`, re-derive `_candidate_state` for the binding triple, persist
  a `ReviewRecord`, redact. Caller-supplied verdict (see Trust boundary).
- `run_independent_review` (requires `allow_bridge_reviewer`; `async with
  project_lock` then `reviews_lock`): re-derive `_candidate_state` *before*,
  `_build_review_spec`, `executor.execute_review`, re-derive `_candidate_state`
  *after*, reject drift (`after != before`), reject a non-completed status,
  `_verdict_from_result` (parse JSON result, or wrap non-JSON as
  `{summary, issues, recommendation, raw}`), `_coerce_verdict`, persist, redact.
- `get_candidate_review` (read-only, `reviews_lock` only): returns the **redacted**
  verdict (the whole response goes through `_redact` with
  `_redact_paths_for_job`, which includes the deterministic worktree path
  `job_worktree_root / project.id / job_id` even though no worktree is resolved —
  so a caller-supplied verdict that echoes the worktree cwd is scrubbed).
- `validate_for_commit` (`reviews_lock` only): exact `project_id`/`job_id` match,
  `used` must be `False`, and `base_head`/`diff_fingerprint`/ordered `paths` must
  equal the verification evidence presented to the commit. Called by the server
  **before** `verification.commit`.
- `mark_used` (`reviews_lock` only): **atomic** — flips `used=True` in memory only
  after the durable row is written; a persistence failure reverts the flag and
  re-raises, so memory and disk never diverge and the review stays reusable.
  Called by the server **after** a successful commit, wrapped best-effort so a
  bookkeeping failure cannot discard the already-succeeded `commit_sha`.

### `src/local_dev_mcp/server.py`

- Imports `ReviewError`/`ReviewManager`; `create_server` takes
  `review: ReviewManager | None = None` and constructs `active_review`.
- Five new tools, each gated
  `registry.resolve(project_id, ProjectCapability.CANDIDATE_REVIEW)`:
  `list_candidate_changes`, `read_candidate_file`, `record_candidate_review`,
  `run_independent_review`, `get_candidate_review`. Tool annotations:
  `list`/`read`/`get` → `readOnlyHint`, not destructive, not openWorld;
  `record` → not read-only, not destructive, not openWorld;
  `run` → not read-only, `openWorld` (it spawns an external Claude session).
- `commit_verified_development_job` gains an optional `review_id`
  (`^`[0-9a-f]{32}$`$`). When `project.requires_review`: a missing `review_id`
  raises `ReviewError`; `validate_for_commit` runs **before** the commit, then
  the commit, then `mark_used` runs **after** a successful commit. When
  `requires_review` is `False` (the default), `review_id` is ignored and the
  commit path is byte-for-byte unchanged.

## Concurrency

Lock ordering is fixed to avoid deadlock: `record_candidate_review` and
`run_independent_review` acquire the **project** lock first, then the
**reviews** lock (`P → R`); `validate_for_commit` and `mark_used` acquire only
the **reviews** lock. There is no `R → P` ordering anywhere, so a wait-for cycle
is impossible. `review.py` imports verification helpers one-way
(`candidate_changed_paths`, `_candidate_state`, `_safe_path`, `_redact`,
`_validate_policy_path`); `verification.py` never imports `review`, so the
server orchestrates `validate_for_commit → commit → mark_used` without an
import cycle. Each manager opens its own connection to the same `bridge.db`;
WAL serializes writers, and every DB write is a brief synchronous call inside
the manager's lock — no `await`-inside-lock, no re-entrancy.

## Security analysis

- **No exposure widening for non-opted-in projects.** Every new surface is gated
  on `candidate_review` (which requires `claude_edit`) or on the policy booleans,
  all `False` by default. A project that does not opt in sees no new tool, no
  schema change, no broadened permission; the commit path is unchanged. The new
  `requires_review → candidate_review` cross-rule makes the "require a review but
  cannot produce one" deadlock unconfigurable (fail-closed at policy load).
- **Reads are scoped and end-to-end anchored.** `read_candidate_file` re-derives
  the changed-path set on every call, requires membership, applies
  `_validate_policy_path`, and reads via `_open_candidate_read` — the leaf is
  opened with `O_NOFOLLOW` relative to a parent `dir_fd` that was walked with
  `O_DIRECTORY|O_NOFOLLOW`, so the open is never re-resolved from the filesystem
  root. A reviewer cannot reach the canonical checkout, follow a symlink (leaf or
  parent — the parent-symlink TOCTOU that a string-returning `_safe_path` left
  open is closed), or read a path outside the candidate's changed set. Reads are
  bounded with a TOCTOU `fstat` re-check on the opened fd. Absolute/`..`/empty/
  non-canonical paths are rejected by `_normalize_review_path`.
- **Redaction, including error paths.** Every output is `_redact`-ed over the
  full path set (worktree path, project root, `job_worktree_root`, config path,
  all project roots); `get_candidate_review` redacts the caller-supplied verdict
  via `_redact_paths_for_job`. The worktree absolute path and the reviewer's
  `session_id` are never returned; `execute_review` produces no `session_id` key.
  Because `_open_candidate_read` opens the leaf by `dir_fd` + relative leaf name,
  an `OSError` from the leaf open carries only the relative leaf in `.filename`,
  never the absolute worktree path. The worktree-**root** `os.open(worktree,
  O_DIRECTORY)` (in both `_open_candidate_read` and `_safe_path`) is wrapped so a
  failure raises a path-free `VerificationError("candidate worktree could not be
  opened")` — an unwrapped `OSError` there would carry the absolute worktree path
  in `.filename` and propagate uncaught past the review layer. Both leak paths
  (leaf and root) are closed at the source, not masked.
- **Separation of duties (spawned reviewer + reserved marker).**
  `run_independent_review` uses `build_reviewer_command`, which asserts
  `resume_session_id is None`, omits `--resume`, restricts tools to
  `Read,Glob,Grep`, and sets `permission-mode default` with no `acceptEdits`. The
  reviewer is a fresh, read-only, non-resumable session; drift between the pre-
  and post-run `_candidate_state` is rejected. It records the reserved
  `reviewer_id="bridge-reviewer"`, which `record_candidate_review` cannot mint
  (rejected by `_validate_reviewer_id`), so an operator inspecting
  `reviewer_id == "bridge-reviewer"` can trust the review was bridge-attested,
  not caller-forged.
- **Single-use, evidence-bound commit gate.** `validate_for_commit` requires an
  exact `(base_head, diff_fingerprint, paths)` match to the verification evidence
  and `used=False`; `mark_used` flips `used` atomically only after a successful
  commit, and the server wraps it best-effort so a bookkeeping failure cannot
  discard the `commit_sha`. The single-use `verification_id` remains the true
  authority: a second commit is blocked by `verification_id` reuse regardless of
  the review's `used` state, so even a `mark_used` that failed to persist cannot
  enable a double-commit.

## Trust boundary (documented, not enforced)

`record_candidate_review` is **caller-supplied**: the bridge binds the supplied
verdict to the candidate's re-derived state and persists it, but it does **not**
attest *who* reviewed. A drafter could in principle call it with its own verdict
and satisfy `requires_review`. This is intentional and documented:

- `record_candidate_review` is the route for an *external* (human or out-of-band)
  reviewer to lodge a verdict through the bridge — the bridge is a courier, not
  an attestor.
- `run_independent_review` is the **strong, attested** guarantee: the bridge
  spawns the reviewer itself, so it can vouch for its freshness, read-only-ness,
  and distinctness from the drafter.

An operator who wants a provably-independent reviewer enables
`allow_bridge_reviewer` and uses `run_independent_review`; an operator who trusts
an external reviewer's channel uses `record_candidate_review`. The two are
complementary. The reserved `reviewer_id="bridge-reviewer"` marker makes the two
auditable: only `run_independent_review` can set it, so a consumer can tell an
attested review from a caller-supplied one by inspecting `reviewer_id`. Attesting
caller identity for `record_candidate_review` (e.g. a reviewer credential) is
explicitly excluded from this milestone; the commit tool's description states
plainly that the gate enforces "a review record bound to this exact candidate
exists", not that the review was independent.

## Risks and limitations (documented, not auto-fixed)

- **Self-review via `record_candidate_review`** (see Trust boundary): the gate
  proves *a* review bound to the candidate's evidence happened, not *who*
  reviewed. Mitigated by `run_independent_review` (attested, reserved
  `reviewer_id`) for operators who need the strong guarantee; documented, not
  identity-enforced.
- **`mark_used` failure after a successful commit:** the review stays reusable
  (atomic revert), but no second commit is possible because `verification_id` is
  single-use. The server surfaces a `review_mark_warning` rather than discarding
  the `commit_sha`; the record may need manual cleanup if an operator re-uses the
  evidence. Safe, not silently corrupting.
- **`record_candidate_review` binding a drifted candidate:** it re-derives
  `_candidate_state` at record time, so the bound triple always matches the
  candidate as it was when recorded; a later commit re-derives again and rejects
  drift, so a stale review cannot authorize a changed candidate.
- **Review single-use is not self-sufficient (latent).** `validate_for_commit`
  releases the reviews lock before the commit; two concurrent commits carrying
  the same `review_id` both pass `validate` seeing `used=False`. This is fully
  mitigated today by `verification.commit`'s project-lock serialization and its
  `current != CandidateState(...)` re-check, plus the single-use
  `verification_id` as the true authority — no double-commit is possible. The
  review layer's own "single-use" invariant rides on that gate; a future change
  that weakened the state re-check would have to re-check this. Documented, not
  re-architected (eager consumption would burn a review on a failed commit and
  need rollback, a worse trade).
- **Long project-lock hold during the spawned reviewer.** `run_independent_review`
  holds the project lock across `execute_review`, serializing all other project
  actions for the review's duration. The subprocess is bounded at the executor
  layer (the same `_run_claude` timeout the drafter uses), so it is not
  unbounded; the cost is availability (a hung reviewer stalls the project), not
  correctness. By design.
- **Open fd on the `ls-tree` failure path (fixed).** `_candidate_state` opens
  the candidate fd *before* the per-path `ls-tree HEAD` baseline call. An
  `ls-tree` raise (timeout or non-zero git) previously leaked that fd, since
  the open was reordered ahead of the baseline check. A `try/finally` now spans
  the open→`os.fdopen` window with a `consumed` flag: the `finally` closes the
  fd unless `os.fdopen` has already taken ownership, so every raise in that
  window (the 120000/160000 symlink/submodule reject, non-regular leaf, and the
  `ls-tree` failure itself) closes the fd. Observable results are unchanged; a
  regression test (`tests/test_verification.py`:
  `test_candidate_state_closes_fd_when_ls_tree_fails`) patches `_git_bytes` to
  fail on `ls-tree` and asserts the tracked fd is closed (`os.fstat` → `EBADF`).
- **Two bridge processes on one `JOB_WORKTREE_ROOT`** remains a pre-existing
  hazard shared with MCPB-006; out of scope here.

## Required tests

Keep all MCPB-001 through MCPB-006 tests passing. Add `tests/test_review.py`
reusing `initialize_git_repository`, the `policy`/`project`/`settings` helpers,
and a fake `ReviewExecutor` that captures calls (asserting
`resume_session_id is None`, supporting drift/status/result-text injection):

1. `list_candidate_changes` shows the changed paths and redacts.
2. `list_candidate_changes` rejects a non-completed job.
3. `read_candidate_file` is scoped to changed paths (rejects `README.md`, `..`,
   absolute).
4. `read_candidate_file` rejects a path outside `commit_roots`.
5. `read_candidate_file` honors a line range and verifies sha256.
6. `read_candidate_file` reports a deletion.
7. `record_candidate_review` binds state and is retrievable.
8. `record_candidate_review` rejects empty/non-dict/oversized verdicts.
9. A review record survives a restart (re-hydrate).
10. `run_independent_review` requires `allow_bridge_reviewer`.
11. `run_independent_review` uses a fresh session (asserts `resume_session_id
    is None`, root = worktree, `reviewer_id == "bridge-reviewer"`, redacts).
12. `run_independent_review` rejects drift.
13. `run_independent_review` rejects a failed status.
14. `run_independent_review` accepts a non-JSON verdict (wraps it).
15. The commit gate requires a review (unknown `review_id` rejected).
16. The commit gate consumes a matching review once (full verify → review →
    commit → `mark_used` → second `validate_for_commit` reports "already been
    used").
17. The commit gate rejects mismatched evidence (fingerprint or paths).
18. The commit gate survives a restart (single-use across re-hydrate).
19. The capability gate: `registry.resolve` raises "does not allow
    candidate_review" for a project without the capability.
20. `get_candidate_review` redacts a verdict that embeds the absolute worktree
    path and project root (the raw paths never appear; the verdict is still
    readable with `<redacted-path>`).
21. `mark_used` failure reverts the in-memory `used` flag and keeps the review
    reusable (memory and disk agree; `validate_for_commit` still passes).
22. `read_candidate_file` rejects a symlink candidate (regression guard for the
    anchored open) — both a symlink leaf and listing the change set.
23. `record_candidate_review` rejects the reserved `reviewer_id="bridge-reviewer"`
    (and a whitespace-padded forgery), while `run_independent_review` still sets it.
24. A root-open failure (`os.open(worktree, O_DIRECTORY)`) does not leak the
    absolute worktree path — the error is the path-free
    `"candidate worktree could not be opened"`.

In `tests/test_config.py` the new `requires_review → candidate_review` cross-rule:
the rule is rejected when `candidate_review` is absent and accepted when both
`verification_commit` and `candidate_review` are present.

Plus `tests/test_server.py` and `tests/test_persistence.py`: the tool count moves
15 → 20; the five review tools are present with the correct read-only /
openWorld annotations; `commit_verified_development_job` advertises `review_id`
with a `requires_review` note. Core tests use temporary repositories and fixed
fake executors; they require no Claude, Ollama, ngrok, network, or real repo.

## Acceptance criteria

- The complete prior and current core suite passes (115 tests: 89 prior + 26
  new — 25 across `tests/test_review.py` and `tests/test_config.py`, plus a
  `_candidate_state` fd-leak regression test in `tests/test_verification.py`).
- Production entry points compile and import at version `0.7.0`.
- FastMCP discovery exposes exactly the intended 20 tools; the five new tools and
  the new `review_id` parameter are inert unless the project opts in.
- Source contains no real `.env`, credentials, certificates, project registry,
  virtual environments, caches, worktrees, or stale build output.
- Live connector, Claude, candidate, and real-repository review smoke checks
  remain separate and are never fabricated.

## Explicitly excluded

- Mandatory review for projects that have not opted in.
- Any new dependency, vector store, or persistence backend (the additive
  `reviews` table reuses `bridge.db`).
- A schema-version bump (the seam is fixed; `SCHEMA_VERSION` stays 1).
- Attesting the identity of a caller that uses `record_candidate_review`.
- Push, merge, tag, branch/ref update, promotion, production overwrite/restart,
  rollback switching, deletion, or worktree cleanup through MCP — unchanged.