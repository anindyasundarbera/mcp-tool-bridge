# MCPB-010 — Continuity release

## Decision

MCPB-006 made the bridge's **terminal/transition** state durable: a job row in
`bridge.db` is upserted when status changes, and a crash mid-flight is honestly
reconciled to `failed`. What it did **not** capture is **in-flight progress** —
`claude.py:_run_claude` is one blocking `communicate()`, so a job killed between
turns has zero durable record of the work it already did, and budget exhaustion
collapses an in-progress run into `failed` alongside candidate failures. Long-
horizon agent work (the Cairn `minimum_unblock`) needs the bridge to treat a
mid-flight stop as a **pause**, not a failure, and to **resume from the last
piece of progress** rather than restarting from scratch.

MCPB-010 adds that **turn-level mission state** as an **internal** extension —
zero new MCP tools, zero new tool parameters, zero broadened permissions,
mirroring MCPB-006's `test_durable_state_adds_no_new_mcp_tools`. The release
delivers:

- **Durable turn-level checkpoints.** A concurrent sampler appends a checkpoint
  row every `CLAUDE_CHECKPOINT_INTERVAL_SECONDS`, and the run loop appends a
  `boundary` checkpoint after every executor slice. Each checkpoint records the
  ordered changed-path set (via `verification.candidate_changed_paths`), a
  SHA-256 `partial_fingerprint` over `base_head` + that set, cumulative usage
  tokens, and the slice index. A crashed job no longer loses its work history.
- **A paused family of states.** `JobStatus` gains `PAUSED_BUDGET_EXHAUSTED`,
  `PAUSED_INTERRUPTED`, `BLOCKED_INPUT_REQUIRED` (resumable) and
  `FAILED_INFRASTRUCTURE`, `QUARANTINED_PROVENANCE` (terminal, non-resumable).
  Budget exhaustion and a stalled run now **pause** instead of failing; only
  genuine candidate failures stay `failed`, so the existing
  checkpoint-less crash-reconciliation test is preserved.
- **Crash reconciliation that preserves progress.** On `_reload_jobs`, a
  non-terminal job **with checkpoint rows** is reconciled to
  `PAUSED_INTERRUPTED` (`pause_reason="bridge restarted while job was in
  progress"`) — resumable from the last checkpoint. A non-terminal job
  **without** checkpoints is reconciled to `failed`, exactly as before. A
  terminal job whose worktree vanished stays `failed`.
- **Soft, multi-dimensional, progress-gated budgets.** `CLAUDE_MAX_TURNS`
  (30) stays the per-slice `--max-turns`. New cumulative budgets —
  `CLAUDE_SOFT_TURNS`, `CLAUDE_EXTENSION_TURNS`, `CLAUDE_MAX_EXTENSIONS`,
  `CLAUDE_HARD_TURNS`, `CLAUDE_MAX_NO_PROGRESS_TURNS`,
  `CLAUDE_MAX_TOOL_FAILURES` — bound the run across turns, tokens, tool
  failures, and no-progress iterations. When Claude returns
  `terminal_reason=="max_turns"`, the bridge grants an **in-place extension
  slice** (re-invoking the executor with `--resume <session_id>` and a fresh
  `--max-turns <extension_turns>`) **only if** the changed-path set grew since
  the last checkpoint, `extensions_granted < max_extensions`,
  `no_progress_turns < max_no_progress_turns`, and `turns_used < hard_turns`.
  A stalled run (`no_progress_turns >= max`) or a hit hard cap pauses as
  `PAUSED_BUDGET_EXHAUSTED`; a tool-failure storm terminates as
  `FAILED_INFRASTRUCTURE`.
- **Resume from the last checkpoint.** `resume_development_job` now accepts any
  `PAUSED_STATUSES` job (not only the historical terminal set). Resuming a
  paused job **keeps the cumulative budget** (`turns_used`/`extensions_granted`
  carry forward) and seeds the drafter prompt from the last checkpoint's
  changed-path set and partial fingerprint, so the next slice continues the
  same Claude session rather than starting over. Non-resumable states
  (`FAILED_INFRASTRUCTURE`, `QUARANTINED_PROVENANCE`) are rejected.
- **A structured terminal report / completion contract.** `get_development_job`
  (`_snapshot`) now always returns `prohibited_actions`, `budget`
  (`turns_used`, `tokens_used`, `extensions_granted`, `no_progress_turns`,
  `tool_failures`, `soft_turns`, `extension_turns`, `max_extensions`,
  `hard_turns`, `max_no_progress_turns`, `soft_warned`), `checkpoint_count`,
  `milestones`, `blockers`, `pause_reason`, and — from the latest checkpoint —
  `candidate_fingerprint`, `changed_path_count`, and `changed_paths`. This is
  the contract an orchestrator checks before promoting a candidate; it is
  additive to the historical snapshot and introduces no new tool.
- **Opt-in stream-json turn counting.** `CLAUDE_STREAM_JSON` switches
  `build_claude_command` to `--output-format stream-json --verbose` and a pure
  `_parse_output` that derives `num_turns`, `tool_failures`, and `usage` from
  the event stream. The default path stays `--output-format json` with the
  sampler driving progress; turn counts are then sample counts, documented
  honestly in the report. The reviewer never streams.

A manual `pause` *tool* is **explicitly deferred**: auto-pause plus the existing
`resume_development_job` is the minimum unblock, and a pause tool would add a
tool surface needing its own authorization. Atomic milestone *parallelization*
is also deferred — this release tracks milestones, it does not parallelize
them.

## Reconciliation

Continuity reuses the established additive-table + re-hydration pattern
(`store.py`), the per-project lock, the durable `bridge.db`, exact-evidence
binding, and `run_git`. It adds **no** capability, **no** tool, **no** tool
parameter, and **no** safety-annotation change. `SCHEMA_VERSION` stays 1: three
new tables — `job_checkpoints`, `job_events`, `job_progress` — are created with
`CREATE TABLE IF NOT EXISTS`, so existing databases upgrade in place (per the
MCPB-006/007/008/009 additive philosophy). The per-job budget fields live in the
**sidecar `job_progress` table** keyed by `job_id` (not `ALTER TABLE` on `jobs`),
so the change is purely additive — no column is added to an existing table.

Progress is defined as a **file-state delta**, reusing `verification.candidate_
changed_paths` and a factored-out `verification.partial_fingerprint`, so the
no-progress counter and progress-gated extensions are robust and
wrapper-agnostic (they do not depend on parsing Claude's internal turn stream).
`DevelopmentJobManager` re-hydrates checkpoints and the progress row
synchronously per job in `__init__`, exactly as MCPB-006 re-hydrates the jobs
table. The sampler task is created and cancelled alongside each executor slice
inside `_run`; it never outlives the run and never cancels the executor itself.

`verification.py` exposes a new lightweight `partial_fingerprint(settings,
worktree, paths, *, project=None)` (and a shared `_digest_candidate_paths`)
factored out of `_candidate_state` so checkpoints can record a content-addressed
snapshot without the full empty-index/commit-root guards. `_candidate_state`
remains the canonical full fingerprint produced at verification time. A
circular import (`verification` imports `jobs`) is resolved by importing these
helpers **lazily** inside the jobs methods that use them.

## Security

Every guard is enforced in server code, never in model instructions:

- **No new authority.** Continuity adds no capability, tool, or parameter. The
  only push surface remains opt-in `push_development_commit`; no merge, tag,
  force-push, trunk update, ref-move, or branch deletion is enabled. The
  snapshot's `prohibited_actions` constant makes this contract explicit to the
  orchestrator: `("merge","tag","force-push","push-to-trunk","branch-deletion",
  "ref-move","release","deploy")`.
- **Extension is resume, not restart.** Each extension slice re-invokes the same
  restricted `build_claude_command` (Read/Glob/Grep/Edit/Write/Bash/mcp__*, no
  `bypassPermissions`) under the same per-project lock, with the same fixed
  argv and `--resume <session_id>`. No new permission mode, no new tool list.
- **Crash safety, not silent loss.** A non-terminal job with checkpoints
  pauses; it is never silently dropped or silently promoted. The hard cap
  (`CLAUDE_HARD_TURNS`) kills a runaway via the existing process-group kill
  rather than letting it run unbounded.
- **No path leakage.** Checkpoint `changed_paths` come from `git` over the
  worktree and are already subject to the existing path guards; the snapshot
  surfaces counts and the changed-path list, never worktree filesystem paths
  beyond what the candidate already exposes.
- **Resume stays scoped.** Resuming a paused job keeps the same pinned
  `base_head` and worktree; `validate_resume` still requires the canonical HEAD
  to equal `base_head` at verify/commit time. A moved base still fails closed.

## Trust boundary (documented)

Continuity is the bridge managing **its own** durable progress state and
budgets. It is **not** the bridge authoring code in its production checkout
(still forbidden; goes through candidate worktrees), and it grants no new
authority to push, merge, tag, or deploy. The model is treated as a replaceable
worker on durable, content-addressed state — pausing and resuming it does not
elevate it. The operator (and ChatGPT confirmation) remain the outer
authorization gate for any commit, push, installation, restart, or live
exercise.

## Explicitly excluded

- A manual `pause` tool or any new tool/parameter/safety-annotation surface —
  continuity is internal to the job lifecycle.
- A schema-version bump; `job_checkpoints`, `job_events`, and `job_progress`
  are additive (`SCHEMA_VERSION` stays 1), and no column is added to the
  existing `jobs` table (the sidecar `job_progress` table holds the new fields).
- Atomic milestone parallelization — milestones are tracked, not parallelized.
- Tool-failure retry/isolation, lost-worktree reconstruction, compromised-
  provenance quarantine machinery beyond the new terminal state — these are P2
  hardening, not the continuity unblock.
- Any change to the reviewer path (`execute_review` never streams, never
  checkpoints; reviewer independence is unchanged).
- Any commit, push, installation, restart, or live-repository exercise without
  separate explicit authorization.