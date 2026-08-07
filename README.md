# Local Development MCP v2

This is the MCPB-005 project-aware ChatGPT-to-Claude Code development bridge.
ChatGPT addresses projects by stable `project_id`; the server resolves those
IDs through a local allowlist and never accepts an arbitrary filesystem root,
worktree path, or Claude session ID from a tool call.

## Capabilities through MCPB-005

- Validated multi-project registry and exact capability authorization
- Safe project discovery and Git status
- Bounded Markdown list, search, full read, range read, outline, and metadata
- Hash-protected Markdown create/update beneath approved write roots
- Hidden, protected, traversal, non-Markdown, non-regular, and symlink access
  denied
- One shared per-project modifying lock; independent projects remain
  independent
- Clean-baseline-gated detached Git worktree per development job
- Asynchronous job IDs, status/results, cancellation, and retained candidates
- Resume only through a bridge-created job with a bridge-captured Claude
  session
- Self-managed candidates started temporarily on a separate localhost port and
  health-checked through MCP
- Registry-fixed verification commands with bounded structured evidence
- SHA-256 candidate fingerprints bound to base HEAD and exact changed paths
- Separate human-approved detached commits of only the unchanged verified state
- Exact staging with `git add -- <paths>`; hooks, signing, and ref updates disabled
- Existing Claude `acceptEdits` boundary with Bash, web, secondary MCP tools,
  slash commands, and `bypassPermissions` disabled

Candidate promotion, production restart, branch/ref updates, merge, tags,
worktree deletion, and rollback switching remain outside bridge authority. The
one push surface is the opt-in `push_development_commit` capability: a single
verified detached commit to a new non-trunk branch on `origin`, never a
force-push, merge, tag, trunk update, or ref move. The one local-checkout
update surface is the opt-in `sync_canonical_checkout` capability: a
fast-forward only of the guarded canonical checkout to the verified current
tip of `origin`'s default branch, never a push, force, merge-commit, tag,
remote ref move, or trunk update.

## MCP tools

Repository and Markdown tools:

- `list_allowed_projects`
- `get_project_status`
- `list_project_markdown`
- `search_project_markdown`
- `read_project_markdown`
- `read_project_file_range`
- `get_markdown_outline`
- `get_project_file_metadata`
- `write_project_markdown`

Isolated development lifecycle:

- `send_to_local_dev` queues a new worktree-isolated job and returns
  immediately with a 32-character job ID.
- `get_development_job` returns changing status and bounded terminal evidence.
- `resume_development_job` continues a terminal job in its retained worktree;
  callers provide the job ID, never a raw session ID.
- `cancel_development_job` stops a queued or running job while retaining its
  worktree and candidate changes.
- `verify_development_job` runs only the fixed command vectors configured for
  the project, then binds passing evidence to the exact unchanged candidate.
- `commit_verified_development_job` creates one detached commit only after a
  separate human approval repeats the verification ID, HEAD, fingerprint, and
  complete ordered path list.
- `push_detached_development_commit` publishes that exact detached commit to a
  new non-trunk remote branch on `origin` after separate human approval repeats
  the commit SHA, base HEAD, and fingerprint. It never force-pushes, merges,
  tags, or moves another ref; one branch per job, with idempotent retry.
- `sync_canonical_checkout` fast-forwards a project's guarded canonical
  checkout to exactly the current tip of `origin`'s default branch after an
  upstream merge, when the checkout is clean and on the default branch and the
  caller repeats the exact current HEAD and target commit. It uses
  `git merge --ff-only`; it never pushes, forces, merges (creates a merge
  commit), tags, or moves a remote ref. It refuses while a non-terminal job
  exists and warns about completed-uncommitted jobs that lose their
  verify/commit path after the base moves.

Candidate review (opt-in `candidate_review`):

- `list_candidate_changes` and `read_candidate_file` expose a retained
  candidate's changed paths to a reviewer, scoped to exactly the changed set.
- `record_candidate_review` records a reviewer verdict bound to the candidate
  state; `get_candidate_review` retrieves it. `run_independent_review` spawns a
  fresh, read-only Claude reviewer distinct from the drafting session.

Job states are `queued`, `preparing_worktree`, `running`, optionally
`validating_candidate`, and one of `completed`, `failed`, `timed_out`, or
`cancelled`. Job and verification records are persisted durably to a local
SQLite database (see [Durable state](#durable-state)) so a server restart
re-hydrates them instead of wiping them.

## Isolation rules

Before a new job starts, the configured checkout must be a clean Git top level,
including no staged or untracked files. The bridge records its exact `HEAD` and
creates a detached worktree beneath:

```text
JOB_WORKTREE_ROOT/<project_id>/<job_id>
```

The worktree root must be private, symlink-free, and outside every configured
project. Claude, verification, and any detached commit operate only in that
worktree. Worktrees remain retained; the bridge exposes no deletion operation.

All modifying bridge operations for one project use the same lock, so a
Markdown write cannot overlap a Claude job. Jobs for different projects can run
independently.

## Durable state

Job and verification records persist to a single local SQLite database so a
server restart no longer amnesiates in-flight or completed work:

```text
JOB_WORKTREE_ROOT/.state/bridge.db
```

The `.state` directory is private (`0o700`) and lives under the same
bridge-owned worktree root, outside every configured project. The database file
is `0o600`, opened with WAL journaling and `synchronous=FULL` so each state
transition commits crash-safely. On startup each manager re-hydrates its records
synchronously before the event loop starts.

Reconciliation is honest, not magical: a job that was not terminal when the
previous process died is reloaded as `failed` (`bridge restarted while job was
in progress`), because its asyncio task and Claude process are gone and cannot
be reattached. A terminal job whose retained worktree directory has since
vanished is reloaded as `failed` (`job worktree no longer exists`). Completed
jobs with a surviving worktree and a bridge-captured Claude `session_id` remain
resumable across restarts, and single-use verification IDs remain valid for a
later commit. Unparseable rows are skipped (fail-closed) so one corrupt record
never blocks startup. A database whose stored schema version is newer than this
code supports is refused on open to avoid silent data loss.

The bridge does not auto-prune an orphaned worktree whose row is missing or
corrupt; manual cleanup is `git -C <project> worktree remove --force <path>`.
Run only one bridge process per `JOB_WORKTREE_ROOT`; two processes would race on
the same database.

## Verification and commit policy

Projects opt in with `verification_commit`, `commit_roots`, and one or more
`verification_commands`. These settings live only in the local registry. MCP
callers cannot supply commands, arguments, environment variables, timeouts,
working directories, or authorization roots. Commands use exact argv vectors
without a shell; `{python}` means the interpreter running the bridge, and an
optional relative `python_path` becomes a confined `PYTHONPATH`.

Verification requires a completed retained job, a clean unchanged source
checkout at the job's base HEAD, an empty candidate index, and changed paths
that are symlink-free, outside protected paths, and beneath `commit_roots`.
Hidden paths require a matching explicit hidden `commit_roots` entry; a broad
`.` root does not authorize them.
Every command must pass without changing the candidate. Output is bounded and
hashed. Success returns a single-use verification ID, the exact changed paths,
and a fingerprint covering the baseline and current file bytes/modes.

Commit is a separate destructive call. The supplied verification ID, base
HEAD, fingerprint, ordered path list, and commit message must match exactly.
The bridge rechecks the candidate, stages only `git add -- <paths>`, proves the
index content matches verification, disables hooks and signing, and creates a
commit on detached HEAD. It never creates or updates a branch or remote ref.

Publication is a separate destructive call gated on the opt-in
`push_development_commit` capability (which requires `verification_commit`).
The supplied job ID, commit SHA, new branch name, base HEAD, and fingerprint
must match exactly. The bridge confirms the commit is the one it created for
that job, re-derives the fingerprint from the commit object, requires a used
verification record for the exact evidence, rejects the default branch,
`main`, and `master`, requires the remote branch not to exist, and pushes
exactly that commit to `refs/heads/<branch>` on `origin` with no force flag. It
never force-pushes, merges, tags, deletes, or moves another ref, and publishes
one branch per job.

Canonical-checkout synchronization is a separate destructive call gated on the
opt-in `sync_canonical_checkout` capability (which requires
`repository_read`). The supplied `expected_current_head` and `target_commit`
must match exactly; the bridge requires the canonical checkout to be clean and
on the repository default branch, fetches `origin`, resolves the default
branch, and requires `target_commit` to equal the current
`refs/remotes/origin/<default>` tip. It advances the local default branch with
`git merge --ff-only <target_commit>`, so it never pushes, forces, creates a
merge commit, tags, deletes, or moves a remote ref. It refuses while a
non-terminal job exists and warns about completed-uncommitted jobs that lose
their verify/commit path after the base moves.

## Self-managed bridge candidates

A `self_managed` project may enable `claude_edit` because development now occurs
in an isolated worktree. It may not enable direct `markdown_write` against the
production checkout.

After a successful self-managed Claude run, the bridge starts the candidate's
`server.py` with `MCP_HOST=127.0.0.1` and `MCP_PORT=CANDIDATE_MCP_PORT`, connects
through MCP, and verifies the lifecycle tool surface. The candidate process is
then stopped and its worktree retained. Candidate startup receives a minimal
allowlisted environment without tokens, certificates, ngrok settings, or
arbitrary production environment variables. A healthy result is a startup
check, not deterministic project verification, and never promotes or restarts
production.

## Limits

| Setting | Default | Purpose |
|---|---:|---|
| `CLAUDE_TIMEOUT_SECONDS` | 1,800 | Maximum Claude run time |
| `CLAUDE_MAX_TURNS` | 30 | Default per-slice Claude turn limit |
| `CLAUDE_SOFT_TURNS` | 30 | Soft warn at this cumulative turn count |
| `CLAUDE_EXTENSION_TURNS` | 30 | Turns granted per in-place extension slice |
| `CLAUDE_MAX_EXTENSIONS` | 3 | Maximum in-place extensions before pausing |
| `CLAUDE_HARD_TURNS` | 120 | Absolute bridge-enforced cumulative turn cap |
| `CLAUDE_MAX_NO_PROGRESS_TURNS` | 6 | Pause when the changed-path set is unchanged for this many turns |
| `CLAUDE_MAX_TOOL_FAILURES` | 5 | Terminate as `failed_infrastructure` above this many tool failures |
| `CLAUDE_CHECKPOINT_INTERVAL_SECONDS` | 30 | Progress-sampler cadence (seconds, may be fractional) |
| `CLAUDE_CHECKPOINT_MAX` | 500 | Checkpoint count that triggers compaction |
| `CLAUDE_STREAM_JSON` | off | Drive Claude with `--output-format stream-json --verbose` for real turn/tool counts |
| `MAX_INPUT_CHARS` | 100,000 | Maximum job instruction length |
| `MAX_OUTPUT_BYTES` | 2,097,152 | Maximum Claude stdout |
| `MAX_MARKDOWN_FILE_BYTES` | 1,048,576 | Maximum reader file size |
| `MAX_MARKDOWN_LIST_RESULTS` | 500 | Maximum Markdown listing results |
| `MAX_MARKDOWN_SEARCH_RESULTS` | 100 | Maximum search results |
| `MAX_MARKDOWN_RANGE_LINES` | 1,000 | Maximum lines per range read |
| `MAX_MARKDOWN_WRITE_BYTES` | 1,048,576 | Maximum Markdown write/current size |
| `MAX_MARKDOWN_DIFF_BYTES` | 262,144 | Maximum returned writer diff |
| `GIT_COMMAND_TIMEOUT_SECONDS` | 30 | Maximum fixed Git command time |
| `CANDIDATE_HEALTH_TIMEOUT_SECONDS` | 30 | Candidate startup/probe deadline |
| `VERIFICATION_COMMAND_TIMEOUT_SECONDS` | 600 | Per-command verification deadline |
| `MAX_VERIFICATION_OUTPUT_BYTES` | 1,048,576 | Captured stdout/stderr per stream |
| `MAX_COMMIT_MESSAGE_CHARS` | 500 | Maximum approved commit message length |

`MCP_PORT` defaults to `8000`; `CANDIDATE_MCP_PORT` defaults to `8001` and must
differ. `JOB_WORKTREE_ROOT` defaults to
`~/.local/state/local-dev-mcp/worktrees`.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
cp projects.example.json projects.json
chmod 600 .env projects.json
```

Keep the real ngrok token only in `.env`. Review `projects.json` before startup.
Only exact IDs and capabilities from that file can be used through MCP. Ensure
`JOB_WORKTREE_ROOT` is outside every configured project root.

## Run

Single command — starts the MCP server and the ngrok tunnel together, with
prefixed logs (`[server]` / `[tunnel]`) and clean shutdown on Ctrl+C:

```bash
./run.sh
```

`run.sh` activates `.venv`, validates `.env`, `projects.json`, and the mTLS CA,
then launches `server.py` and `tunnel.py` as child processes. When
`NGROK_DOMAIN` is set in `.env` (e.g. `swivel-aide-overprice.ngrok-free.dev`)
the tunnel binds to that reserved domain for a stable connector URL; otherwise
ngrok issues an ephemeral URL that changes every run.

The two can also be run separately in two terminals:

```bash
python server.py
```

```bash
python tunnel.py
```

The connector URL remains `https://<ngrok-domain>/mcp`. The ngrok endpoint is
protected by the OpenAI connector mTLS CA; Ollama is never exposed directly.

## Test

Core tests have no network, model, ngrok, Ollama, or real-repository dependency:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

After the server is running, the separate live smoke test lists tools, checks a
project, starts an isolated no-edit job, and polls its result:

```bash
python test_client.py --project cairn-knowledge-store
```
