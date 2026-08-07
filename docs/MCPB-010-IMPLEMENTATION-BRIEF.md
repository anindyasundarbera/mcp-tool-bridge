# MCPB-010 — Continuity release (implementation brief)

## Objective

Add durable **turn-level mission state** to the development-job lifecycle so a
mid-flight stop is a **pause** (resumable from the last piece of progress) rather
than a failure, and so a long-horizon run is bounded by soft, multi-dimensional,
progress-gated budgets. **Internal only** — zero new MCP tools, zero new tool
parameters, zero broadened permissions — mirroring MCPB-006's
`test_durable_state_adds_no_new_mcp_tools`.

Read alongside `docs/MCPB-010.md` (the decision record).

## Architecture

### `src/local_dev_mcp/store.py`

Three additive tables (no schema-version bump; `CREATE TABLE IF NOT EXISTS`,
existing `bridge.db` upgrades in place):

```sql
CREATE TABLE IF NOT EXISTS job_checkpoints (
    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,            -- sample|boundary|extension|milestone
    seq INTEGER NOT NULL,          -- monotonic per-job index
    recorded_at TEXT NOT NULL,
    changed_paths_json TEXT NOT NULL,
    partial_fingerprint TEXT,      -- sha256 over base_head + current changed set
    tokens_json TEXT,              -- cumulative usage tokens
    event_json TEXT,               -- sampler summary or slice metadata
    milestone TEXT                 -- milestone id this checkpoint closes, if any
);
CREATE TABLE IF NOT EXISTS job_events (        -- append-only audit ledger
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,      -- created|status|worktree_prepared|extension|pause|terminal
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_progress (     -- sidecar for per-job budget fields
    job_id TEXT PRIMARY KEY,
    pause_reason TEXT,
    turns_used INTEGER NOT NULL,
    tokens_used INTEGER NOT NULL,
    extensions_granted INTEGER NOT NULL,
    no_progress_turns INTEGER NOT NULL,
    tool_failures INTEGER NOT NULL,
    soft_warned INTEGER NOT NULL,
    milestones_json TEXT,
    updated_at TEXT NOT NULL
);
```

The per-job budget fields live in the **sidecar `job_progress` table** keyed by
`job_id` rather than as new columns on `jobs`, so no `ALTER TABLE` touches an
existing table — the change is purely additive.

New helpers (all reuse the `with connection:` ACID pattern):
`insert_checkpoint_row`, `load_checkpoint_rows(job_id)`,
`load_latest_checkpoint(job_id)`, `checkpoint_count(job_id)`,
`compact_checkpoints(job_id, keep_kinds=("boundary","extension","milestone"),
keep_latest_n=50)` (deletes old `sample`/`turn`/`tool` rows, retains all
boundary/extension/milestone rows plus the most recent `keep_latest_n`),
`insert_event_row`, `load_event_rows(job_id)`, `upsert_progress_row`,
`load_progress_row`.

### `src/local_dev_mcp/config.py`

- New `_positive_float` helper (mirrors `_positive_int` with `float(raw)`),
  used so `CLAUDE_CHECKPOINT_INTERVAL_SECONDS` accepts sub-second values for
  responsive local runs and fast tests.
- New budget settings via the existing helpers, defaults from the roadmap's
  `minimum_unblock`:

| Var | Type | Default | Purpose |
|---|---|---:|---|
| `CLAUDE_SOFT_TURNS` | int | 30 | soft warn at this cumulative turn count |
| `CLAUDE_EXTENSION_TURNS` | int | 30 | turns granted per extension slice |
| `CLAUDE_MAX_EXTENSIONS` | int | 3 | max in-place extensions |
| `CLAUDE_HARD_TURNS` | int | 120 | absolute bridge-enforced cumulative cap |
| `CLAUDE_MAX_NO_PROGRESS_TURNS` | int | 6 | pause when changed-set unchanged this many turns |
| `CLAUDE_MAX_TOOL_FAILURES` | int | 5 | terminate→`failed_infrastructure` above this |
| `CLAUDE_CHECKPOINT_INTERVAL_SECONDS` | float | 30 | sampler cadence |
| `CLAUDE_CHECKPOINT_MAX` | int | 500 | checkpoint count that triggers compaction |
| `CLAUDE_STREAM_JSON` | bool | false | drive Claude with `--output-format stream-json --verbose` |

`CLAUDE_MAX_TURNS` (30) stays the per-invocation `--max-turns` for the first
slice; later slices use `CLAUDE_EXTENSION_TURNS`.

### `src/local_dev_mcp/claude.py`

- `build_claude_command(..., max_turns_override: int | None = None)`:
  `max_turns_override` (must be > 0) takes precedence over the project's
  `claude_max_turns`. `--output-format` is `stream-json` when
  `settings.claude_stream_json` else `json`; `--verbose` is appended only in
  stream mode. `build_reviewer_command` is unchanged (reviewer never streams).
- New pure `_parse_output(stdout: str, *, stream: bool) -> dict`: json mode does
  `json.loads` with an `{"is_error": True, "result": stdout}` fallback; stream
  mode iterates newline-delimited events, finds the `type=="result"` envelope,
  counts `assistant` turns, counts `user` tool_result `is_error` items, skips
  malformed lines, and returns `{"is_error": True, "result": stdout,
  "num_turns": N}` if no result event is present.
- `_run_claude(..., stream: bool = False)` passes `stream` through to
  `_parse_output`; non-stream empty results fall back to the historical
  `{"is_error": ..., "result": stdout}` shape.
- `execute(..., max_turns: int | None = None)` passes `max_turns_override` and
  `stream=self.settings.claude_stream_json`; the response now carries `usage`,
  `num_turns`, and `tool_failures`. `execute_review` passes `stream=False`.

### `src/local_dev_mcp/verification.py`

- New `partial_fingerprint(settings, worktree, paths, *, project=None)` and a
  shared `_digest_candidate_paths(..., validate_policy)` factored out of
  `_candidate_state` so checkpoints can record a content-addressed snapshot
  without the full empty-index/commit-root guards. `_candidate_state` remains
  the canonical full fingerprint at verification time. `candidate_changed_paths`
  and `VerificationError` are the public symbols `jobs.py` reuses.

### `src/local_dev_mcp/jobs.py`

- **`JobStatus`**: add `PAUSED_BUDGET_EXHAUSTED`, `PAUSED_INTERRUPTED`,
  `BLOCKED_INPUT_REQUIRED`, `FAILED_INFRASTRUCTURE`, `QUARANTINED_PROVENANCE`
  (lowercase StrEnum values). `TERMINAL_STATUSES` is extended to include all
  five (paused jobs are not running). `PAUSED_STATUSES = {PAUSED_BUDGET_EXHAUSTED,
  PAUSED_INTERRUPTED, BLOCKED_INPUT_REQUIRED}` (resumable).
  `NON_RESUMABLE_STATUSES = {FAILED_INFRASTRUCTURE, QUARANTINED_PROVENANCE}`.
  `PROHIBITED_ACTIONS = ("merge","tag","force-push","push-to-trunk",
  "branch-deletion","ref-move","release","deploy")`. Existing `failed` is kept
  for genuine candidate failures.
- **`DevelopmentJob`**: new fields `pause_reason`, `turns_used`, `tokens_used`,
  `extensions_granted`, `no_progress_turns`, `tool_failures`, `soft_warned`,
  `milestones`. `__init__` adds `self._last_changed_paths: dict[str, tuple]`.
- **Imports**: the top-level `from .verification import ...` is removed
  (`verification` imports `jobs`, so it is circular); `candidate_changed_paths`
  /`VerificationError` and `partial_fingerprint` are imported **lazily** inside
  the methods that use them (`_current_changed_paths`, `_record_checkpoint`).
- **`_reload_jobs`**: a non-terminal job **with** `store.checkpoint_count > 0`
  → `PAUSED_INTERRUPTED` (`pause_reason="bridge restarted while job was in
  progress"`); **without** checkpoints → `failed` (unchanged). Each job has
  `_apply_progress_row` applied and mutated jobs `_persist_progress`-ed.
- **`resume`**: accepts `PAUSED_STATUSES` (via `TERMINAL_STATUSES`); rejects
  `NON_RESUMABLE_STATUSES` with "this development job is not resumable". A
  paused job keeps the cumulative budget, clears `pause_reason`, and seeds via
  `_resume_seed(checkpoint)`; a terminal job resets the budget. `resume_seed`
  is threaded to `_run`.
- **`_run`** (rewritten, `resume_seed: str = ""`): builds `drafter_text` (spec
  + seed when it fits in `max_input_chars`); on resume seeds the
  `_last_changed_paths` baseline from the last checkpoint so progress detection
  compares against the paused candidate. An extension loop sets `slice_max`
  (`claude_max_turns` for a fresh first slice, `claude_extension_turns` for
  resume and all later slices), creates `exec_task` and a `sampler`
  (`_sampler_loop`), and in `try/finally` cancels + drains both. After each
  slice: `_absorb_result_budget`, `_record_checkpoint("boundary")`, capture
  `session_id` on **every** slice (under lock), `_persist_progress`,
  `_maybe_compact`. Decision order: completed → `COMPLETED` (with the
  self-managed-bridge-edit check); `timed_out` → `TIMED_OUT`;
  `tool_failures >= max` → `FAILED_INFRASTRUCTURE`; `no_progress_turns >= max`
  → `PAUSED_BUDGET_EXHAUSTED`; `turns_used >= hard` → `PAUSED_BUDGET_EXHAUSTED`;
  if `terminal_reason == "max_turns"` and `extensions_granted < max` and
  `no_progress < max` and `turns < hard` and `_progressed` → grant extension
  (`extensions_granted += 1`, `"extension"` checkpoint, continue); `claude_status
  == "failed"` → `FAILED`; else `PAUSED_BUDGET_EXHAUSTED`.
  `CancelledError` → `CANCELLED`; other `Exception` → `FAILED`; finally persists
  job + progress.
- **Helpers**: `_progress_to_row`, `_persist_progress`, `_apply_progress_row`,
  `_record_event`, `_accumulate_tokens`, `_current_changed_paths` (wraps
  `candidate_changed_paths`, returns `()` on `VerificationError`/exception),
  `_record_checkpoint` (computes changed paths + `partial_fingerprint`,
  advances `no_progress_turns` by comparing to `_last_changed_paths`, inserts
  the row, returns paths), `_progressed`, `_sampler_loop` (every
  `claude_checkpoint_interval_seconds`, appends `sample` checkpoints, never
  cancels the executor), `_absorb_result_budget` (accumulates `num_turns`/
  `usage` tokens/`tool_failures`, sets `soft_warned`), `_maybe_compact`,
  `_resume_seed`.
- **`_snapshot`**: `resume_available` now excludes `NON_RESUMABLE_STATUSES`;
  always adds `prohibited_actions`, `budget` (the full budget object),
  `checkpoint_count`, `milestones`, `blockers`, `pause_reason`, and from the
  latest checkpoint `candidate_fingerprint`/`changed_path_count`/
  `changed_paths`. This is the structured terminal report — additive, no new
  tool.
- **`start`** persists progress + records a `"created"` event; `_run` records
  `"worktree_prepared"` (not a duplicate `"created"`).

## Concurrency

Each slice runs `exec_task` and `sampler` concurrently. The sampler appends
`sample` checkpoints on its own cadence and **never cancels the executor**; the
`try/finally` cancels and drains both. All shared-state mutations
(`session_id`, progress fields, status) happen under `_state_lock`. The
per-project lock is held for the whole run as before. Checkpoint and progress
writes are single-statement ACID transactions in `bridge.db` (WAL,
`synchronous=FULL`).

## Security analysis

The threat model is "a mid-flight stop silently discards work, a stalled run
runs unbounded, or budget/extension grants new authority." Each is blocked:

- **No new authority.** Zero new capabilities/tools/params; the snapshot's
  `prohibited_actions` makes the no-merge/tag/force/trunk contract explicit.
- **Extension is resume, not elevation.** Every slice reuses the restricted
  `build_claude_command` (no `bypassPermissions`, fixed argv) under the same
  per-project lock with `--resume <session_id>`.
- **Bounded run.** The hard cap kills a runaway via the existing process-group
  kill; no-progress and tool-failure caps terminate cleanly.
- **No silent loss.** A non-terminal job with checkpoints pauses; it is never
  silently dropped or promoted.
- **Resume stays pinned.** Resuming keeps the same `base_head`/worktree;
  `validate_resume` still fails closed at verify/commit if the base moved.

## Risks and limitations (documented, not auto-fixed)

- **Turn/turn-count accuracy.** In the default (non-stream) path, turn counts
  are sample counts, documented honestly in the report; the stream-json path
  derives real `num_turns` from the event stream when `CLAUDE_STREAM_JSON` is
  set. Token capture is best-effort from Claude's `usage` field.
- **Sampler cadence.** A very short `CLAUDE_CHECKPOINT_INTERVAL_SECONDS` is
  useful for tests but adds checkpoint write load in production; the default
  of 30s is conservative.
- **Compaction.** `compact_checkpoints` prunes only `sample`/`turn`/`tool`
  rows; promoted evidence (verifications/reviews/publications) is never
  touched.

## Required tests

`tests/test_checkpoints.py` (new), `unittest` with fake executors (mirror
`tests/test_jobs.py`). All fake executor `execute` methods gain a
`max_turns: int | None = None` kwarg.

- `test_checkpoints_appended_and_rehydrated`;
- `test_progress_grants_extension_and_then_hard_cap_pauses`
  (`MAX_EXTENSIONS=2`, `HARD_TURNS=100`, `num_turns=40` → 3 calls,
  `extensions_granted=2`, hard cap);
- `test_no_progress_terminates_to_paused` (interval=0.02, max_no_progress=3);
- `test_crashed_job_with_checkpoints_becomes_paused_interrupted`;
- `test_crashed_job_without_checkpoints_still_failed` (preserves existing
  behavior);
- `test_paused_job_resumes_from_last_checkpoint` (asserts resume seed text +
  `resume_session_id`);
- `test_non_resumable_terminal_states_reject_resume` (quarantined_provenance);
- `test_terminal_report_contract_fields`;
- `test_token_capture`;
- `test_soft_budget_warning_flagged`.
- `CheckpointStoreTests`: `test_compaction_keeps_milestone_and_boundary_checkpoints`
  (60 samples + 2 boundary + 1 milestone → compact(keep_latest_n=10) keeps 13),
  `test_additive_schema_upgrades_in_place` (drop continuity tables, re-open
  recreates them, legacy job survives, `SCHEMA_VERSION=1`).
- `ContinuityToolSurfaceTests.test_continuity_adds_no_new_mcp_tools` (asserts
  `len(tools) == 22`, no pause/checkpoint/extend/compact/list_checkpoints tools).

`tests/test_claude.py`: `ContinuityCommandTests` (5: max_turns override +
  positivity, default json, stream-json + verbose, reviewer stays json) and
  `ParseOutputTests` (6: json parse, json fallback, stream result envelope,
  stream turn/tool-failure counts, stream skips malformed, stream no-result is
  error). `tests/test_jobs.py`, `tests/test_verification.py`,
  `tests/test_server.py` extended so fake executors accept `max_turns`.

## Acceptance criteria

- `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests` is green —
  167 tests (143 prior + 11 new `test_claude` + 13 new `test_checkpoints`).
- `list_tools()` returns **22** tools — unchanged from MCPB-009; continuity adds
  zero tools, parameters, or safety annotations.
- A pre-continuity `bridge.db` upgrades in place: the three new tables are
  created, legacy jobs survive, `SCHEMA_VERSION` stays 1.
- Not committed, pushed, installed, restarted, or exercised against a live
  repository without separate explicit authorization.

## Explicitly excluded

- A manual `pause` tool or any new tool/parameter/safety-annotation surface.
- A schema-version bump; the three new tables are additive and no column is
  added to the existing `jobs` table (sidecar `job_progress` holds the fields).
- Atomic milestone parallelization (milestones are tracked, not parallelized).
- Tool-failure retry/isolation, lost-worktree reconstruction, and quarantine
  machinery beyond the new terminal states (P2 hardening).
- Any change to the reviewer path.
- Any commit/push/installation/restart/live exercise without explicit
  authorization.