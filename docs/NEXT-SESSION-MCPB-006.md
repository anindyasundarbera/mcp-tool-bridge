# Next session handoff — review MCPB-006

Updated: 2026-08-06

## Authoritative project

- GitHub: `https://github.com/anindyasundarbera/mcp-tool-bridge`
- Branch: `feature/updatingCapabilities`
- Accepted and committed MCPB-005 baseline:
  `aa78e2b24b38ac0ed7cc9312a13ad96be22176ac`
  (`MCPB-005 — Deterministic verification and approved commit`)
- ChatGPT project/application: `mcp-chatgpt-plugin`
- The prepared MCPB-006 source is local only. It has not been committed,
  pushed, installed on the live bridge, restarted, or exercised through a
  modifying live connector tool. No milestone ZIP has been built for MCPB-006.

## Motivation

Before MCPB-006, all job and verification state lived only in process memory.
Restarting the bridge wiped every job and verification record, so an
orchestrator holding a `job_id` across a restart got `unknown job_id` from every
lifecycle tool — even though the on-disk detached git worktree and its candidate
changes survived. The orphaned CKS-002B job
`b84bca300d24ceb4fece6770c7e90dab` (worktree intact at base `76cb66f`) was the
concrete instance that prompted this phase. Recovery of that specific orphan is
deferred; MCPB-006 fixes the bridge so the class of failure cannot recur.

## Prepared MCPB-006 implementation

- Added `src/local_dev_mcp/store.py`: a focused SQLite persistence layer
  (stdlib `sqlite3`, no new dependency). Three tables — `schema_meta`, `jobs`,
  `verifications` — spoken to in plain dict rows. WAL journal mode,
  `synchronous=FULL`, `foreign_keys=ON`, `temp_store=MEMORY`,
  `busy_timeout=2000`; `0o600` database file under a `0o700` `.state` directory.
  A stored schema version newer than supported fails closed on open.
- Added `GitWorktreeManager.ensure_state_root()`: validates that
  `JOB_WORKTREE_ROOT/.state` is outside every configured project root, then
  creates the private directory.
- Wired `DevelopmentJobManager` to re-hydrate synchronously in `__init__` and
  persist on every mutation inside the existing state lock. Non-terminal jobs
  are reconciled to `failed` (`bridge restarted while job was in progress`);
  terminal jobs whose worktree directory has vanished are reconciled to
  `failed` (`job worktree no longer exists`). `_new_job_id` also checks the
  database.
- Wired `VerificationCommitManager` to re-hydrate in `__init__` and persist
  records inside the existing records lock. Verification records are atomic;
  `used` is preserved as-is, so single-use IDs survive restart.
- The `store` module imports only `worktrees`; row-to-object mappers live in
  `jobs.py`/`verification.py` where the types are defined, avoiding an import
  cycle.
- Package version is `0.6.0`.

## Changed files from MCPB-005

Modified:

- `README.md`
- `pyproject.toml`
- `src/local_dev_mcp/__init__.py`
- `src/local_dev_mcp/jobs.py`
- `src/local_dev_mcp/verification.py`
- `src/local_dev_mcp/worktrees.py`

Added:

- `docs/MCPB-006.md`
- `docs/MCPB-006-IMPLEMENTATION-BRIEF.md`
- `docs/NEXT-SESSION-MCPB-006.md`
- `src/local_dev_mcp/store.py`
- `tests/test_persistence.py`

(`AGENTS.md` is also updated: roadmap row, MCPB-006 section, and the current
resume pointer.)

## Verification completed

- The MCPB-005 baseline (`aa78e2b`) passed all 61 existing tests before edits.
- The complete MCPB-006 suite passes all 70 tests (61 prior + 9 new) with
  temporary repositories and fixed fake executors. No network, Claude, Ollama,
  ngrok, or real user repository is required.
- New `tests/test_persistence.py` covers: completed-job survival and
  session-id reuse across restart; non-terminal → failed reconciliation;
  unparseable-row skip with other rows intact; unknown schema version refuses
  to start; deleted-worktree → failed with `resume_available=False`;
  verification record survival and single-use replay rejection across restart;
  committed job not resumable after reload; `attempt`/`created_at`/`effort`
  round-trip; no new MCP tools (still 15, `resume` still takes `job_id` not
  `session_id`); and `synchronous=FULL` + `journal_mode=WAL` pragmas set.
- FastMCP still exposes exactly 15 tools with unchanged schemas and safety
  annotations.
- Production entry points import at version `0.6.0`.

Live Claude/Ollama, candidate-server, ngrok, real-repository restart
verification, installation, and restart checks remain separate and must not be
inferred from core tests.

## Review boundary

MCPB-006 adds no MCP tool, tool parameter, schema, or safety-annotation change,
no permission broadening, no auto-pruning of worktrees, no
push/merge/tag/branch-ref/promotion/production-overwrite/restart/rollback/
deletion/worktree-cleanup operation, and no new dependency. The only new
on-disk artifact is the private SQLite state database under the bridge-owned
worktree root.

## Known limitations (documented, deferred)

- An orphaned worktree whose database row is missing or corrupt is not
  auto-pruned; manual cleanup is `git -C <project> worktree remove --force
  <path>`.
- No advisory `flock`/exclusive-locking guard against two bridge processes on
  one `JOB_WORKTREE_ROOT`; WAL serializes writes and the single-instance
  assumption is documented. Revisit as a follow-up.
- A `_persist_job`/`_persist_record` database error propagates to the caller;
  the in-memory record may diverge until the next successful write. Not
  swallowed.
- Schema migration tooling beyond the `schema_meta` version seam is deferred
  (v1 only).

## Next actions

1. Review the prepared source, the implementation brief, and this handoff.
2. If accepted, explicitly authorize any desired GitHub commit/push.
3. Separately install/restart the live bridge and run a real restart-survival
   smoke test if desired: start a job, restart the bridge process, confirm
   `get_development_job` returns the reloaded job (not `unknown job_id`) and
   that `resume`/`verify`/`commit` proceed.
4. Recovery of the orphaned CKS-002B job
   `b84bca300d24ceb4fece6770c7e90dab` is a separate, deferred task — it predates
   the durable store and has no database row to re-hydrate.
5. A final reviewed source ZIP, if requested, is produced after acceptance with
   an exact file inventory and SHA-256.