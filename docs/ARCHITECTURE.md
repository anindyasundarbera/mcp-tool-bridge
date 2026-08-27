# Architecture

This document is the mental model of the Local Development MCP bridge
(BridgeLab). Read it to understand how a request flows from a remote
assistant, through the registry and locks, into an isolated worktree, through
verification, and out to a bounded publication — and how every step is enforced
in server code.

For the tool-by-tool surface, see [TOOLS.md](TOOLS.md). For the authorization
model, see [CAPABILITIES.md](CAPABILITIES.md). For the security rules, see
[SECURITY.md](SECURITY.md).

---

## 1. System context

```
                         ┌─────────────────────────────────────────────┐
   ChatGPT (remote)      │  This repository (the bridge)                 │
   OpenAI connector ─────▶│                                                │
   mTLS over HTTPS       │  tunnel.py   ngrok ── mTLS terminate ──┐      │
                         │                                                ▼      │
                         │  server.py   FastMCP HTTP  127.0.0.1:8000  /mcp │
                         │      │                                         │
                         │      ▼  resolves project_id → ProjectPolicy    │
                         │  ProjectRegistry (projects.json)               │
                         │      │                                         │
                         │      ▼  per-project asyncio.Lock              │
                         │  DevelopmentJobManager / VerificationCommit …  │
                         │      │                                         │
                         │      ▼  detached git worktree (clean baseline) │
                         │  JOB_WORKTREE_ROOT/<project_id>/<job_id>        │
                         │      │                                         │
                         │      ▼                                         │
                         │  Claude Code (local, acceptEdits only)         │
                         │                                                │
                         │  .state/bridge.db  (SQLite, WAL, sync=FULL)     │
                         └─────────────────────────────────────────────┘
```

The public surface is a single ngrok HTTPS endpoint protected by the OpenAI
connector mTLS CA. Only the MCP server port is exposed; the configured Claude
launcher and the local filesystem are never directly reachable. Everything
behind the endpoint is a FastMCP HTTP server on localhost.

---

## 2. Core principles

These are not preferences — they are invariants the code enforces:

1. **Registry as the only authorization source of truth.** `projects.json` is
   loaded once at startup into a frozen, validated `ProjectRegistry`. Every
   tool call resolves a `project_id` to a `ProjectPolicy` and checks the
   required `ProjectCapability`. Unknown, disabled, or unauthorized → fail
   closed.
2. **No caller-supplied authority.** A tool caller provides `project_id`,
   `job_id`, content, and exact-match confirmations (HEAD, fingerprint, paths).
   They never provide a filesystem root, worktree path, raw session ID,
   executable, argv, environment, working directory, or timeout.
3. **Isolation by detached worktree.** Modifying work happens in a detached git
   worktree created from a clean-baseline canonical checkout, beneath a private
   bridge-owned root outside every project. The canonical checkout is never
   edited directly (except opt-in `sync_canonical_checkout`, which is
   fast-forward only).
4. **Fixed command vectors, no shell.** Every external command (verification,
   runtime verification, supply-chain scans, git helpers) runs with a fixed
   argv via `asyncio.create_subprocess_exec` / `subprocess.run` — never
   `shell=True`. `{python}` expands to `sys.executable` and is allowed only as
   `argv[0]`.
5. **Separate human approvals for destructive steps.** Verify, commit, push,
   and sync are each their own MCP call. Commit requires re-stating the
   verification ID, base HEAD, fingerprint, and ordered path list. Push
   requires re-stating the commit SHA, base HEAD, and fingerprint. Sync requires
   re-stating the current HEAD and target commit.
6. **Durable, honest state.** Every state transition persists to SQLite
   crash-safely. Restart reconciliation is honest: in-flight jobs are not
   magically resumed as running — they are `failed` (or `paused_interrupted`
   if checkpoints exist).
7. **Bounded everything.** Inputs, outputs, files, searches, diffs, execution
   time, model turns, and budget dimensions are all capped by `Settings`.

---

## 3. Request flow

A typical modifying request — `send_to_local_dev` — flows like this:

```
MCP call(project_id, text, effort, requested_base?)
  │
  ▼
server.py  create_server() closure
  │   active_registry.resolve(project_id, CLAUDE_EDIT)   ← capability gate
  ▼
DevelopmentJobManager.start(project, text, effort, requested_base)
  │   async with active_locks.for_project(project.id):   ← per-project lock
  │     ├─ stale-base guard (reads local remote-tracking refs only)
  │     ├─ record exact canonical HEAD
  │     ├─ create detached worktree under JOB_WORKTREE_ROOT/<project>/<job_id>
  │     ├─ persist job row (status=queued) → bridge.db  (ACID)
  │     └─ spawn Claude Code subprocess (acceptEdits; Bash/web/MCP off)
  ▼
returns immediately: { job_id (32-hex), status, ... }
```

The caller then polls `get_development_job`, which returns a bounded, sanitized
snapshot (no worktree path, no raw session ID). The job runs to a terminal or
paused state. Each transition re-persists the job row and appends an audit
event to `job_events`.

A **verify → commit → publish** chain is three independent calls:

```
verify_development_job(project_id, job_id, expected_head)
  → VerificationCommitManager.verify()
      recheck clean unchanged checkout at base HEAD, empty candidate index,
              symlink-free / in-commit-roots / non-protected changed paths
      run each registry-fixed verification command (fixed argv, no shell)
      every command must pass without changing the candidate
      success → single-use verification_id bound to (HEAD, fingerprint, paths)

commit_verified_development_job(project_id, job_id, verification_id,
                                 expected_head, expected_diff_fingerprint,
                                 paths, message, review_id?)
  → recheck candidate, stage only `git add -- <paths>`,
    prove index matches verification, disable hooks+signing,
    commit on detached HEAD. Never creates/updates a branch or ref.
  → (requires_review projects also consume a single-use review_id)

push_detached_development_commit(project_id, job_id, commit_sha, branch_name,
                                   expected_base_head, expected_diff_fingerprint)
  → PublicationManager.publish()
      confirm commit is the one it created for that job,
      re-derive fingerprint from the commit object,
      require a used verification record for the exact evidence,
      reject default / main / master, require remote branch absent,
      push <commit>:refs/heads/<branch> with no force flag.
      One branch per job; idempotent retry.
```

Each step is a **confirmation of the prior step's exact state**, not a
continuation. The bridge re-derives fingerprints rather than trusting caller
assertions.

---

## 4. Components (module map)

All bridge code lives in `src/local_dev_mcp/`. Dependency direction is one-way:
`server` → managers → `store` → `worktrees`; `store` imports only `worktrees`
(no import cycle).

### Entry points (repo root)
| File | Role |
|---|---|
| `server.py` | Loads `.env`, inserts `src/` on `sys.path`, calls `local_dev_mcp.server.run()` |
| `tunnel.py` | Establishes the ngrok HTTPS endpoint with OpenAI mTLS termination |
| `run.sh` | Launches server + tunnel with prefixed logs, port-ownership probe, clean shutdown |
| `test_client.py` | Live smoke client (lists tools, checks a project, runs a no-edit job) |

### Bridge modules
| Module | Role |
|---|---|
| `server.py` | `create_server()` — assembles all managers and registers the 39 MCP tools with FastMCP `ToolAnnotations` |
| `config.py` | `Settings.from_env()` — every tunable limit, validated as positive int/float/port |
| `models.py` | `ProjectCapability` enum, `ProjectPolicy` / `ProjectRegistryDocument` Pydantic models, command validators, capability-dependency rules |
| `registry.py` | `ProjectRegistry.load()` (validates JSON, resolves roots, dedups, requires git) and `ProjectLockManager` (per-project `asyncio.Lock`) |
| `git.py` | `project_status()` and git helpers; fixed argv, no shell; `GitCommandError` |
| `worktrees.py` | Worktree creation/cleanup, clean-baseline checks, `validate_resume`, private-dir helpers |
| `jobs.py` | `DevelopmentJobManager` — start/get/resume/cancel/list, checkpointing, budget enforcement, crash reconciliation, structured `_snapshot` |
| `claude.py` | `ClaudeExecutor` — builds the `claude` argv from the configurable `CLAUDE_LAUNCH_COMMAND`/`CLAUDE_REVIEW_LAUNCH_COMMAND` templates (substituting `{model}`), appends the bridge-enforced security tail, drives json/stream-json modes, parses turns/tool-failures/usage, reviewer command |
| `candidate.py` | Candidate state helpers (changed paths, fingerprints) |
| `reader.py` | `RepositoryReader` — bounded Markdown list/search/read/range/outline/metadata; injection-defense option |
| `writer.py` | `RepositoryWriter` — SHA-256-guarded create/update beneath approved write roots |
| `verification.py` | `VerificationCommitManager` — fixed-command verification, diff fingerprints, single-use IDs, exact-path staging + detached commit |
| `review.py` | `ReviewManager` — candidate change listing/file read, verdict recording, independent reviewer spawn |
| `pr.py` | `PullRequestManager` — race-safe draft PR create/update via `gh` (no merge) |
| `sync.py` | `SyncManager` — `prepare_canonical_for_work` (fetch + verdict, no merge) and `sync` (ff-only to `origin/<default>`) |
| `evidence.py` | `EvidenceBundleManager` — redacted, content-addressed portable evidence bundle |
| `runtime_verification.py` | `RuntimeVerificationManager` (MCPB-011) — one policy-named host-runtime check, no shell, immutability recheck |
| `working_tree_capture.py` | `WorkingTreeCaptureManager` (MCPB-012) — capture approved dirty-tree changes into a retained candidate without touching the source |
| `open_hands_action.py` | `OpenHandsActionManager` — governed disposable-runtime action with OH-001D ledger |
| `introspection.py` | (M-AL-01) `effective_policy_view`, `CanonicalizationResolver` — read-only observation |
| `authorization.py` | (M-GV-01) durable per-action authorization objects, single-use consumption |
| `requirement.py` | (M-EV-01) requirement traceability (normative ID extraction + links) |
| `store.py` | **The only module that knows the on-disk schema.** SQLite open/migrate, upsert/load helpers for all 20 tables |

### Tests
`tests/` holds 340+ `unittest` tests with no network, model, ngrok, launcher, or
real-repository dependency. Managers are driven through fake executors so
lifecycle behavior is deterministic. A few tool-count canary tests assert the
exact tool surface and must be bumped when a tool is added.

---

## 5. The registry and capability model

`projects.json` is loaded at startup into a frozen `ProjectRegistryDocument`
(Pydantic, `extra="forbid"`). Each `ProjectPolicy` carries:

- `id`, `display_name`, `description`, `root` (absolute, must be the git top
  level when `require_git`),
- `capabilities` — a `frozenset[ProjectCapability]` (15 possible),
- `markdown_write_roots`, `commit_roots`, `protected_paths`,
- `verification_commands`, `supply_chain_scans`, `runtime_verification_commands`,
  `working_tree_capture_scope`,
- flags: `self_managed`, `requires_review`, `allow_bridge_reviewer`,
  `default_effort`, `allow_ultracode`, per-project `claude_model` /
  `claude_max_turns`.

The `validate_policy` model validator enforces **capability dependencies**
(e.g. `verification_commit` requires `claude_edit` + a non-empty
`commit_roots` + ≥1 `verification_commands`; `push_development_commit` requires
`verification_commit`; `sync_canonical_checkout` / `export_evidence_bundle` /
`runtime_verification` / `working_tree_candidate_capture` require
`repository_read`; `open_pull_request` requires `push_development_commit`;
introspection capabilities require their prerequisite). This means an
invalid `projects.json` fails at startup, not at first use. Full rules in
[CAPABILITIES.md](CAPABILITIES.md).

`ProjectLockManager` gives each project one `asyncio.Lock`, so all modifying
operations for a project serialize (a Markdown write cannot overlap a Claude
job), while independent projects proceed concurrently.

---

## 6. Isolation rules

Before a new job starts, the configured checkout must be a clean Git top level
— no staged or untracked files. The bridge records its exact `HEAD` and creates
a detached worktree beneath:

```
JOB_WORKTREE_ROOT/<project_id>/<job_id>
```

The worktree root must be private, symlink-free, and outside every configured
project. Claude, verification, and any detached commit operate only in that
worktree. Worktrees are **retained** — the bridge exposes no deletion operation
(orphan cleanup is manual: `git -C <project> worktree remove --force <path>`).

A stale-base guard reads **local remote-tracking refs only** (no `git fetch` on
the hot path) when no `requested_base` is pinned: if the canonical checkout is
behind `origin/<default>`, `send_to_local_dev` refuses with an actionable
message naming `prepare_canonical_for_work(pull=true)` rather than silently
branching new work off a stale base. The guard is armed by an earlier
`prepare_canonical_for_work` fetch; if no one has fetched, it no-ops. It is
skipped for local-only repos with no `origin` and when `requested_base` pins a
commit. A `diverged` checkout still requires a human steward.

---

## 7. Durable state

State lives in one SQLite database:

```
JOB_WORKTREE_ROOT/.state/bridge.db     file 0o600, dir 0o700
```

Opened with `journal_mode=WAL`, `synchronous=FULL`, `foreign_keys=ON`,
`busy_timeout=2000`. `SCHEMA_VERSION` stays 1; all schema changes are
**additive** (`CREATE TABLE IF NOT EXISTS` + idempotent `ALTER TABLE ADD
COLUMN` guarded by `PRAGMA table_info`). A stored schema version newer than the
code is refused on open (fail-closed). `store.py` is the *only* module that
knows the schema; managers own the object↔row mapping.

### Tables (20)
| Table | Purpose | Mutation |
|---|---|---|
| `schema_meta` | schema version | guarded |
| `jobs` | one row per development job | upsert per transition |
| `verifications` | single-use verification evidence | upsert |
| `reviews` | single-use review verdicts | upsert |
| `publications` | one published branch per job | upsert (keyed project+job) |
| `syncs` | canonical-sync audit | append-only |
| `job_checkpoints` | turn-level mission state | append-only; compacted |
| `job_events` | audit ledger of every transition | append-only |
| `job_progress` | budget counters + pause reason + milestones | upsert sidecar |
| `delegations` | parent→child job delegation | append-only |
| `requirements` | normative requirement IDs | upsert |
| `requirement_links` | requirement ↔ job links | append-only |
| `authorizations` | per-action authorization objects | upsert; single-use consume |
| `source_classifications` | fixed authority vocabulary | seeded |
| `authority_records` | authority behind each governance event | append-only |
| `supply_chain_scans` | commit-time scan audit | append-only |
| `runtime_verifications` | host-runtime check evidence (`rtv_` IDs) | append-only |
| `working_tree_captures` | capture attempt audit | append-only |
| `oh_ledger` | disposable-runtime action ledger | append-only |

### Reconciliation (honest, not magical)
- Non-terminal job at crash time, **no checkpoints** → reloaded `failed`
  ("bridge restarted while job was in progress").
- Non-terminal job at crash time, **with checkpoints** → reloaded
  `paused_interrupted` (resumable from the last checkpoint).
- Terminal job whose worktree directory has vanished → reloaded `failed`
  ("job worktree no longer exists").
- Completed job with surviving worktree and a bridge-captured `session_id`
  stays resumable; single-use verification/review IDs stay valid.
- Unparseable rows are skipped (fail-closed) so one corrupt record never blocks
  startup.

Run only **one** bridge process per `JOB_WORKTREE_ROOT`; two would race on the
same database. The bridge does not auto-prune orphaned worktrees.

---

## 8. Budgets and continuity

`jobs.py` enforces multi-dimensional budgets driven by `Settings`:

- **Soft / hard turn budgets**: `CLAUDE_SOFT_TURNS` (warn), `CLAUDE_HARD_TURNS`
  (absolute cap), `CLAUDE_EXTENSION_TURNS` + `CLAUDE_MAX_EXTENSIONS` (in-place
  `--resume <session_id>` extension slices).
- **Progress gating**: `CLAUDE_MAX_NO_PROGRESS_TURNS` — pause when the
  changed-path set is unchanged for this many turns (progress is a changed-path
  delta reusing `verification.candidate_changed_paths` +
  `partial_fingerprint`). A progressing run gets an extension slice; a stalled
  run pauses.
- **Tool-failure storm**: `CLAUDE_MAX_TOOL_FAILURES` — terminate as
  `failed_infrastructure` above this many tool failures (genuine candidate
  failures stay `failed`).
- **Checkpoints**: `CLAUDE_CHECKPOINT_INTERVAL_SECONDS` cadence,
  `CLAUDE_CHECKPOINT_MAX` triggers `compact_checkpoints` (keeps all
  boundary/extension/milestone rows + the most recent N sample rows).
- **Streaming**: opt-in `CLAUDE_STREAM_JSON` drives Claude with
  `--output-format stream-json --verbose` for real turn/tool counts; the
  default path stays `json` with the sampler driving progress (turn counts are
  then sample counts). The reviewer never streams.

`resume_development_job` accepts `paused_*` jobs, keeps the cumulative budget,
and seeds the drafter from the last checkpoint's changed-path set;
non-resumable states are rejected. The structured terminal report in
`get_development_job` (`_snapshot`) always surfaces `prohibited_actions`, the
full `budget` object, `checkpoint_count`, `milestones`, `blockers`,
`pause_reason`, and the latest checkpoint's fingerprint / changed paths.

---

## 9. Verification & commit policy (detail)

Projects opt in with `verification_commit`, `commit_roots`, and ≥1
`verification_commands`. `MCPB-006`'s `test_durable_state_adds_no_new_mcp_tools`
discipline applies: persistence is an internal side-effect of existing
mutations, never a new tool surface.

`VerificationCommand` (`models.py`):
- fixed `argv` (1–64 args, NUL-free, ≤2000 chars), validated by
  `_validate_command_argv`; `{python}` only as `argv[0]`, expanded to
  `sys.executable`; shell executables (`sh`/`bash`/`dash`/`zsh`/`ksh`/`fish`/
  `powershell`/`pwsh`/`cmd`) rejected;
- optional relative `python_path` (≤16 entries) → confined `PYTHONPATH`;
- optional `sandbox` flag (M-CC-11: hermetic, repo-controlled verification
  scripts).

Verification requires a completed retained job, a clean unchanged checkout at
the job's base HEAD, an empty candidate index, and changed paths that are
symlink-free, outside protected paths, and beneath `commit_roots`. Hidden paths
require a matching explicit hidden `commit_roots` entry; a broad `.` root does
not authorize them. Every command must pass without changing the candidate.
Output is bounded and hashed. Success returns a single-use `verification_id`,
the exact changed paths, and a fingerprint over baseline + current file
bytes/modes.

Commit rechecks the candidate, stages only `git add -- <paths>`, proves the
index content matches verification, disables hooks and signing, and creates a
commit on detached HEAD — never a branch or ref update. For
`requires_review` projects, a matching single-use `review_id` is consumed; the
gate enforces that a review record bound to this exact candidate exists (it
does not mandate independence — that is the operator's channel choice:
`run_independent_review` attested as reviewer_id `bridge-reviewer`, versus a
caller-supplied `record_candidate_review`).

---

## 10. Publication & canonical sync

**Publication** is gated on the opt-in `push_development_commit` capability
(requires `verification_commit`). It rejects the repository default branch,
`main`, and `master`; requires the remote branch not to exist; pushes exactly
`<commit>:refs/heads/<branch>` on `origin` with no force flag; never
force-pushes, merges, tags, deletes, or moves another ref; one branch per job
with idempotent retry. The commit's fingerprint is re-derived from the commit
object, and a used verification record for the exact evidence is required.

**Canonical sync** is gated on the opt-in `sync_canonical_checkout`
capability (requires `repository_read`). It requires a clean checkout on the
repository default branch; fetches `origin`; requires `target_commit` to equal
the current `refs/remotes/origin/<default>` tip; advances the local default
branch with `git merge --ff-only <target_commit>`. It never pushes, forces,
creates a merge commit, tags, deletes, or moves a remote ref. It refuses while
a non-terminal job exists and warns about completed-uncommitted jobs that lose
their verify/commit path after the base moves. `prepare_canonical_for_work` is
the read-only prelude: fetch + verdict (`up_to_date` / `updates_available` /
`ahead` / `diverged` / `dirty` / `no_origin` / `fetch_failed`) without merging;
`pull=true` fast-forwards to the auto-discovered remote tip.

---

## 11. Opt-in host-runtime surfaces

Three capabilities are dormant by default and exist for checks that cannot run
inside an isolated Claude worktree:

- **`runtime_verification` (MCPB-011)** — one policy-named check against the
  clean canonical checkout at an exact expected HEAD. The caller supplies only
  `project_id` / `check_id` / `expected_head`. Executed via
  `asyncio.create_subprocess_exec` (no shell), `start_new_session=True`, with a
  minimal deterministic environment (the bridge's full process env is **not**
  inherited — no token/cert leakage); `PATH` is allowed, interpreter/loader
  injection keys are forbidden. Read-only git only; repository immutability is
  rechecked after execution (any drift → `failed`, no auto-restore).
  Content-addressed evidence with `rtv_`-prefixed IDs (a separate namespace
  from candidate `verification_id`s, so a runtime ID can never authorize a
  commit). Evidence-only: never stages/commits/pushes/merges/tags/moves a ref.
- **`working_tree_candidate_capture` (MCPB-012)** — capture an approved set of
  uncommitted working-tree changes from a *dirty* canonical checkout into a
  retained candidate **without modifying the source**. The include/protected-
  exclude scope is pinned in trusted policy; the caller's lists must equal it
  exactly as sets; protected exclusions always override inclusions and never
  appear in the candidate. A detached candidate worktree is created at the bound
  HEAD and overlaid with only the selected, fingerprinted changes; the source
  working-tree fingerprint is recomputed before success and any drift fails the
  capture. The retained candidate is a normal governed candidate eligible for
  verify/review/commit/publish.
- **`governed_disposable_runtime_action_execution`** — a restricted OpenHands
  action in an isolated disposable runtime, bound to a canonical baseline,
  tools restricted to TerminalTool + FileEditorTool, with an OH-001D ledger.

---

## 12. Self-managed bridge candidates

A `self_managed` project may enable `claude_edit` because development occurs in
an isolated worktree. It may **not** enable direct `markdown_write` against the
production checkout. After a successful self-managed Claude run, the bridge
starts the candidate's `server.py` with `MCP_HOST=127.0.0.1` and
`MCP_PORT=CANDIDATE_MCP_PORT`, connects through MCP, and verifies the lifecycle
tool surface. The candidate process is then stopped and its worktree retained.
Candidate startup receives a minimal allowlisted environment without tokens,
certificates, ngrok settings, or arbitrary production variables. A healthy
result is a startup check, not deterministic project verification, and never
promotes or restarts production.

---

## 13. Threat model (summary)

- **Untrusted remote caller** (the assistant or anything that can reach the
  endpoint): constrained by mTLS at the ngrok edge, then by registry
  authorization, exact-match confirmations, and the impossibility of supplying
  executables/argv/env/cwd/timeout. Fail-closed everywhere.
- **Prompt injection via repository content**: opt-in `READER_INJECTION_DEFENSE`
  fences + delimits untrusted repo content the reader ingests; repo-controlled
  verification scripts run sandboxed (M-CC-11); a severity-gated supply-chain
  scan can reject commits (M-CC-11, e.g. `grype --fail-on=high`).
- **Stale base / base drift**: `prepare_canonical_for_work` + the stale-base
  guard prevent new work from branching off a stale `HEAD`;
  `validate_resume` requires the canonical HEAD to still equal a job's
  `base_head` before verify/commit.
- **Race on destructive steps**: single-use verification/review IDs are
  consumed atomically (`WHERE used = 0`); publication is one-branch-per-job with
  idempotent retry; per-project locks serialize modifying operations.
- **Restart amnesia**: WAL + `synchronous=FULL` + synchronous re-hydration +
  honest reconciliation (in-flight → `failed`/`paused_interrupted`).
- **Self-restart / self-push**: the running bridge must not directly edit its
  production checkout — its own changes require the MCPB-004 candidate-worktree
  workflow.

Full non-negotiable rules: [SECURITY.md](SECURITY.md).