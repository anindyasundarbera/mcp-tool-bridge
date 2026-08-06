# MCPB-006 — Durable job and verification state (restart survival)

## Objective

Before this phase, `DevelopmentJobManager._jobs` and
`VerificationCommitManager._records` were in-memory dicts rebuilt fresh in
`create_server()` on every startup. Any server restart wiped every job and
verification record, so an orchestrator holding a `job_id` across a restart
received `unknown job_id` from every lifecycle tool — even though the on-disk
detached git worktree and its candidate changes survived perfectly.

MCPB-006 makes job and verification state durable so a restart re-hydrates
records from disk, re-adopts surviving worktrees, honestly reconciles jobs that
were mid-flight at crash time, and preserves every existing fail-closed security
boundary — with no new tools and no permission broadening.

The package version for this milestone is `0.6.0`.

MCPB-006 does not add any MCP tool, change any tool schema or safety annotation,
broaden any permission, auto-prune worktrees, or introduce a dependency.

## Storage backend

SQLite via the Python stdlib `sqlite3` module — zero new dependencies. ChromaDB
was considered and rejected: it is a vector/embedding database for RAG, the
wrong model for transactional job state, and would add a heavy dependency.
SQLite gives ACID transactions, a single database file, trivial future
audit/cleanup/listing queries, and stronger crash-safety than per-file JSON.

A single database lives under a private, bridge-owned `.state/` directory beneath
the existing `job_worktree_root` (already `0o700`, already enforced outside every
project root by `GitWorktreeManager._root_for`):

```text
<job_worktree_root>/.state/        # 0o700, bridge-owned (no project content)
  bridge.db                        # 0o600, WAL journal mode
  bridge.db-wal / -shm             # protected by the 0o700 directory
```

## Architecture

### `src/local_dev_mcp/store.py` (new — focused persistence layer)

The only module that knows the on-disk schema. It speaks plain `dict` rows; the
managers own the object-to-row mapping. It imports only `worktrees`
(`_ensure_private_directory`), keeping the dependency direction one-way
(`jobs`/`verification` import `store`; `store` imports only `worktrees`) and
avoiding the import cycle that a first draft hit by importing the job/verification
types.

- `SCHEMA_VERSION = 1`.
- `ensure_state_dir(state_root) -> Path`: create/validate `.state` (0o700) via
  `_ensure_private_directory` (O_NOFOLLOW per component, rejects group/other).
- `open_state_db(db_path) -> sqlite3.Connection`:
  `sqlite3.connect(path, check_same_thread=False)`; `row_factory = sqlite3.Row`;
  pragmas `journal_mode=WAL`, `synchronous=FULL` (durable commits — the point of
  this phase), `foreign_keys=ON`, `temp_store=MEMORY`, `busy_timeout=2000`
  (belt-and-suspenders; writes are already serialized by the managers' locks).
  Create the schema idempotently; write `SCHEMA_VERSION` into `schema_meta`
  (`ON CONFLICT DO NOTHING`); read it back and raise `StateStoreError` if the
  stored version is newer than supported ("refusing to start to avoid data
  loss"). `os.chmod(db_path, 0o600)` after creation.
- `load_job_rows`, `load_record_rows`: return `list[dict]`.
- `upsert_job_row(conn, row)`, `upsert_record_row(conn, row)`: each
  `INSERT … ON CONFLICT DO UPDATE …` inside `with conn:` (one ACID transaction).
- `job_id_exists(conn, job_id) -> bool`: defense-in-depth for ID allocation.
- `class StateStoreError(RuntimeError)`.

### Schema

```sql
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, effort TEXT NOT NULL,
  status TEXT NOT NULL, attempt INTEGER NOT NULL, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, cancel_requested INTEGER NOT NULL, session_id TEXT,
  committed_sha TEXT, worktree_path TEXT, worktree_base_head TEXT,
  result_json TEXT, candidate_json TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS verifications (
  verification_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, job_id TEXT NOT NULL,
  base_head TEXT NOT NULL, diff_fingerprint TEXT NOT NULL, paths_json TEXT NOT NULL,
  verified_at TEXT NOT NULL, used INTEGER NOT NULL, commands_json TEXT NOT NULL);
```

No foreign key between `verifications.job_id` and `jobs.job_id`: jobs can be
reconciled or dropped, and an FK would complicate reconciliation. `result`,
`candidate`, `paths`, and `commands` are JSON-text columns. `status`/`effort`
are reconstructed by the managers' row mappers (`JobStatus(value)`, validated
against `{"high","ultracode"}`); `cancel_requested`/`used` are 0/1. The live
asyncio `task` is never stored.

### `src/local_dev_mcp/worktrees.py`

Added `GitWorktreeManager.ensure_state_root() -> Path`: validates that
`settings.job_worktree_root / ".state"` is outside every root in
`self._protected_project_roots` (reusing the `_is_within` containment logic from
`_root_for`), then `_ensure_private_directory` it. With `projects=()` (some
tests) the check passes trivially, matching `_root_for`. Keeps the project-root
containment invariant explicit at construction.

### `src/local_dev_mcp/jobs.py`

- Module-level helpers before the manager: `_VALID_EFFORTS`, `_json_dumps`,
  `_json_loads`, `job_to_row(job)`, `job_from_row(row) -> DevelopmentJob | None`
  (returns None on unknown status/effort or malformed required fields, building
  a `JobWorktree` from `worktree_path`+`worktree_base_head`), and
  `_worktree_dir_ok(path)` (sync `os.lstat`, rejects symlink/non-dir/missing).
- In `__init__`, after `self._state_lock = asyncio.Lock()`:
  `self._db = store.open_state_db(self._worktrees.ensure_state_root() / "bridge.db")`
  then `self._reload_jobs()`. Reload is synchronous (filesystem/DB only, no
  `await`/git) so it works before the event loop starts and inside the
  `asyncio.run(...)` test pattern.
- `_reload_jobs()`: loads rows, skips None, sets `job.task = None`, reconciles
  non-terminal → `failed` (`bridge restarted while job was in progress`) or
  missing-worktree → `failed` (`job worktree no longer exists`), upserts changed
  rows.
- `_persist_job(job)`: `store.upsert_job_row(self._db, job_to_row(job))`, called
  inside `async with self._state_lock` at every mutation site: `start`,
  `resume`, `cancel` (both blocks), `record_commit`, `_set_status`, and every
  `_run` state mutation (worktree assignment, result/session, candidate/status,
  the CancelledError branch, the Exception branch, and the finally `task=None`).
  `get` and `verification_worktree` are read-only.
- `_new_job_id`: also rejects a candidate present in the database via
  `store.job_id_exists`.

### `src/local_dev_mcp/verification.py`

- Helpers after the `VerificationRecord` dataclass: `_record_json_loads`,
  `record_to_row(record)`, `record_from_row(row) -> VerificationRecord | None`
  (returns None if `paths`/`commands` are not lists or a required field is
  missing).
- In `__init__`, after `self._records_lock = asyncio.Lock()`:
  `self._db = store.open_state_db(self._worktrees.ensure_state_root() / "bridge.db")`
  (its own connection to the same database file; WAL supports multiple
  connections) then `self._reload_records()`.
- `_reload_records()`: loads rows, skips None, stores into `self._records`. No
  reconciliation — records are atomic/terminal; `used` is preserved as-is.
- `_persist_record(record)`: `store.upsert_record_row(...)`, called inside
  `async with self._records_lock` at verify-success (after
  `self._records[...] = record`) and at commit (after `record.used = True`).

### `src/local_dev_mcp/server.py`, `config.py`

No change. Reload happens in manager `__init__`, which `create_server()` already
runs. No new `Settings` field: `state_root` is derived as
`settings.job_worktree_root / ".state"`. Each manager opens its own connection
to the same `bridge.db`; WAL plus the managers' locks make this safe.

## Concurrency

The two managers (jobs + verification) open separate connections to the same
`bridge.db`. WAL serializes writers; readers see the latest committed snapshot.
All DB writes happen inside the managers' existing `async with ...lock` blocks
as brief synchronous calls — no `await`-inside-lock, no re-entrancy. An advisory
`flock` / `PRAGMA locking_mode=EXCLUSIVE` guard against two bridge processes on
one `JOB_WORKTREE_ROOT` is deliberately deferred: it would complicate
`create_server`, which constructs two managers in one process opening two
connections to the same database. The single-instance-per-root assumption is
documented instead.

## Security analysis

- **No exposure widening.** `session_id` moves from process memory to a `0o600`
  SQLite file under a `0o700` `.state` directory under the already-`0o700`
  worktree root — readable only by the same OS user. No new reader is introduced.
- **No new tools, no permission broadening, no schema changes** to any tool.
  Persistence is an internal side-effect of existing mutations.
- **Fail-closed preserved:** an unparseable row → `job_from_row`/`record_from_row`
  return None → row skipped → `get` returns `unknown job_id`; the server still
  starts; other rows intact. A missing worktree on reload → `failed`,
  `worktree=None`. Full base-HEAD and integrity re-validation still happens at
  use time via the unchanged `validate_resume`.
- **"Bridge-created session resume only" not weakened:** `resume` still requires
  `job.session_id is not None`, set only from a bridge-captured,
  `_valid_session_id`-checked `captured_session`. Callers still pass `job_id`,
  never `session_id`. `_snapshot` never emits `session_id`.
- **Redaction on persist is already correct:** `result`/`candidate`/`error`
  pass through `_redact(...)` before assignment to the job. The only raw
  absolute path persisted is `worktree.path` (required to re-adopt the worktree;
  `0o600`, never tool-exposed).
- **DB file permissions:** `bridge.db` is `0o600`; the `-wal`/`-shm` sidecars are
  protected by the `0o700` directory regardless of their own mode.

## Risks and limitations (documented, not auto-fixed)

- **Orphaned worktree with no database row** (crash between `git worktree add`
  and the worktree-assignment persist) → reloaded as `failed` with
  `worktree=None`; the on-disk git worktree leaks. Not auto-pruned — deleting a
  valid worktree whose row is merely absent/corrupt is worse. Manual cleanup:
  `git -C <project> worktree remove --force <path>`.
- **Two bridge processes on one `JOB_WORKTREE_ROOT`** (pre-existing hazard) would
  race on the same `bridge.db`. An advisory `flock`/exclusive-locking guard is
  deferred (see Concurrency). WAL serializes writes; the single-instance
  assumption is documented; revisit as a follow-up.
- **`_persist_job` DB error** (disk full, locked) propagates to the tool caller
  as a 500; memory is already mutated, so that one record may diverge until the
  next successful write. Not swallowed.
- **Schema evolution:** `schema_meta` + `SCHEMA_VERSION` give a migration seam.
  On open, a stored version higher than supported fails closed. v1 only; a future
  v2 reader can fall back to v1.

## Required tests

Keep all MCPB-001 through MCPB-005 tests passing (fresh temp
`JOB_WORKTREE_ROOT` → `.state/bridge.db` created empty → reload loads nothing →
behavior unchanged). Add `tests/test_persistence.py` reusing
`ImmediateExecutor`/`BlockingExecutor`/`EditingExecutor`,
`initialize_git_repository`, `policy(...)`/`project(...)` from existing tests;
simulate "restart" by constructing a new manager instance with the same settings
in the same process (each new manager opens its own connection to the same
`bridge.db`):

1. Completed job survives restart; `resume` on the reloaded manager reuses the
   persisted `session_id` (`resume_session_id == "bridge-session-1"`).
2. Non-terminal persisted job → `failed` on reload, error
   `bridge restarted while job was in progress`.
3. Unparseable row → skipped; server starts; other valid jobs intact (insert a
   row with `status='bogus'` via a raw `sqlite3` connection).
4. Unknown schema version → reload refuses to start (insert `schema_meta`
   version `999` → `StateStoreError`).
5. Worktree deleted between runs → `failed` on reload,
   `job worktree no longer exists`, `resume_available=False`.
6. Verification record survives restart and remains single-use: `commit` on the
   reloaded manager succeeds once; a second `commit` raises `already been used`.
7. Committed job is not resumable after reload (`resume` raises "a committed
   development job cannot resume").
8. `attempt`/`created_at`/`effort` round-trip faithfully after a resume.
9. FastMCP tool discovery unchanged (still 15 tools; no persistence-related tool
   leaked; `resume_development_job` still takes `job_id`, never `session_id`).
10. SQLite durability sanity: `synchronous=FULL` and `journal_mode=WAL` set
    (assert via `PRAGMA` reads).

Core tests use temporary repositories and fixed fake executors. They require no
Claude, Ollama, ngrok, network, or real user repository.

## Acceptance criteria

- The complete prior and current core suite passes (70 tests: 61 prior + 9 new).
- Production entry points compile and import at version `0.6.0`.
- FastMCP discovery still exposes exactly the intended 15 tools with unchanged
  schemas and safety annotations.
- Source contains no real `.env`, credentials, certificates, project registry,
  virtual environments, caches, worktrees, or stale build output.
- Live connector, Claude, candidate, and real-repository restart smoke checks
  remain separate and are never fabricated.

## Explicitly excluded

- Any new MCP tool, tool parameter, schema, or safety-annotation change.
- Auto-pruning of orphaned worktrees.
- An advisory `flock`/exclusive-locking single-instance guard.
- Schema migration tooling beyond the `schema_meta` version seam.
- Push, merge, tag, branch/ref update, promotion, production overwrite/restart,
  rollback switching, deletion, or worktree cleanup through MCP.