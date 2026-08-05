# MCPB-004 — Isolated asynchronous development

## Objective

Move every Claude editing request out of the configured project checkout and
into a bridge-created detached Git worktree. A request starts an asynchronous
job and returns an opaque job ID immediately. The bridge exposes bounded status,
result, continuation, and cancellation operations for that job.

For the self-managed bridge project, a successful Claude run is only a staged
candidate. The bridge starts that candidate on a separate localhost port,
performs an MCP health probe, stops the candidate process, and retains the
worktree for review. It never overwrites or restarts production, advances a Git
ref, merges, commits, pushes, or switches to the candidate.

The package version for this milestone is `0.4.0`.

## Tool contracts

### Start an isolated job

`send_to_local_dev(project_id, text, effort="high")` keeps its existing name
but becomes asynchronous. It resolves the exact project with `claude_edit`,
creates an opaque job record, queues the job behind the shared per-project
modifying lock, and returns a snapshot containing at least:

```json
{
  "job_id": "32 lowercase hexadecimal characters",
  "project_id": "cairn-knowledge-store",
  "status": "queued",
  "effort": "high",
  "attempt": 1,
  "created_at": "UTC timestamp",
  "updated_at": "UTC timestamp",
  "resume_available": false
}
```

The tool never waits for Claude to finish and never returns an absolute
worktree path.

### Inspect a job

`get_development_job(project_id, job_id)` returns the current job snapshot.
Terminal snapshots include bounded Claude output, repository before/after
evidence from the isolated worktree, and candidate-health evidence when
applicable. The raw Claude session ID is never returned.

### Continue a bridge-created session

`resume_development_job(project_id, job_id, text, effort="high")` is allowed
only for a terminal job which has a session ID captured by this bridge. The
caller supplies the job ID, never a Claude session ID. The same job worktree is
re-queued, its attempt number increments, and Claude receives the stored
session ID through a fixed argument vector.

Continuation fails closed if the job belongs to another project, is active,
has no bridge-captured session, has lost its worktree, or the configured source
checkout is no longer clean at the job's original base HEAD.

### Cancel a job

`cancel_development_job(project_id, job_id)` cancels a queued, preparing,
running, or candidate-validating job. Running Claude and candidate-server
process groups receive termination and are killed after a bounded grace period.
Cancellation preserves the worktree and its changes. Repeated cancellation of
a terminal job is safe and returns the current snapshot.

## Job lifecycle

```text
queued
  -> preparing_worktree
  -> running
  -> validating_candidate (self-managed jobs only)
  -> completed | failed | timed_out | cancelled
```

- Jobs are stored in memory for this phase. A server restart invalidates job
  IDs and resume authority but does not delete retained worktrees.
- One shared per-project modifying lock serializes Markdown writes, worktree
  preparation, Claude execution, continuation, cancellation finalization, and
  candidate validation. Independent projects can progress independently.
- Worktrees are retained after every terminal outcome for inspection and the
  MCPB-005 verification/commit workflow. MCPB-004 exposes no deletion tool.

## Worktree invariants

1. Resolve the project and `claude_edit` capability before creating a job.
2. The configured project root must be the Git top level.
3. Immediately before worktree creation, require `git status --porcelain=v1
   --untracked-files=all` to be empty and record the exact `HEAD`.
4. Create a collision-resistant bridge job ID; callers never choose worktree
   names or paths.
5. Create the worktree beneath `JOB_WORKTREE_ROOT/<project_id>/<job_id>` with
   `git worktree add --detach -- <path> <head>` using a fixed argument vector.
6. Require the new worktree to be clean and at the recorded base HEAD before
   Claude starts.
7. Recheck that the source checkout stayed clean and at the same HEAD through
   worktree preparation. A failed check leaves the source checkout unchanged.
8. Never create a branch, update a ref, stage, commit, merge, push, tag, or
   delete a retained worktree.
9. Never expose the absolute source or worktree path in a tool response.

`JOB_WORKTREE_ROOT` must resolve outside every configured project root. The
bridge creates controlled job directories with private permissions and rejects
symlinked or non-directory worktree-root components.

## Claude execution and resume

- Claude runs with its current `acceptEdits` boundary and fixed
  `Read,Glob,Grep,Edit,Write` allowlist inside the job worktree.
- Bash, web tools, secondary MCP tools, slash commands, and
  `bypassPermissions` remain disabled.
- The selected model, effort, turn limit, input limit, output limit, and timeout
  remain enforced.
- Cancellation and timeout terminate the complete subprocess group.
- A resume command adds exactly one `--resume <stored-session-id>` pair. Public
  tool input has no session-ID parameter.
- Session IDs are accepted only from a successful, bounded Claude JSON result
  produced for the same bridge job.

## Self-managed candidate workflow

A self-managed project may now enable `claude_edit` because that capability is
worktree-isolated. It still may not enable direct `markdown_write` against its
production checkout.

After a successful self-managed Claude run:

1. Keep the candidate as uncommitted changes in its detached worktree.
2. Start `<current-python> server.py` from that worktree with a minimal
   allowlisted environment, the exact configured registry path,
   `MCP_HOST=127.0.0.1`, and `MCP_PORT=CANDIDATE_MCP_PORT`. Do not pass tokens,
   certificates, arbitrary environment variables, or ngrok settings to the
   candidate.
3. Require the candidate port to differ from the production MCP port.
4. Connect locally through the MCP client, initialize the server, list tools,
   and require the MCPB-004 lifecycle tools to be present.
5. Bound startup/probe time with `CANDIDATE_HEALTH_TIMEOUT_SECONDS` and bound
   captured error output.
6. Stop the candidate process group after the probe, whether healthy or not.
7. Return only `healthy`/`unhealthy`, the localhost port, discovered tool names,
   and bounded diagnostics. Do not expose paths or environment values.

A healthy probe proves only that the candidate starts and serves its expected
MCP surface. Deterministic project verification remains MCPB-005 work.

## Configuration

Add and document:

```dotenv
JOB_WORKTREE_ROOT=~/.local/state/local-dev-mcp/worktrees
GIT_COMMAND_TIMEOUT_SECONDS=30
CANDIDATE_MCP_PORT=8001
CANDIDATE_HEALTH_TIMEOUT_SECONDS=30
```

All numeric limits must be positive; ports must be at most 65535 and the
candidate port must differ from `MCP_PORT`.

## MCP metadata

- `send_to_local_dev`: read-write, destructive, non-idempotent, open-world.
- `get_development_job`: read-only, non-destructive, non-idempotent because the
  observed state can change, closed-world.
- `resume_development_job`: read-write, destructive, non-idempotent,
  open-world.
- `cancel_development_job`: read-write, destructive, idempotent, closed-world.

All `job_id` inputs use an exact 32-character lowercase hexadecimal schema.

## Required tests

Keep all 36 MCPB-001 through MCPB-003 tests passing and add coverage for:

- clean source checkout creates a detached worktree at the exact base HEAD;
- dirty, staged, untracked, non-Git, symlinked-root, collision, and Git-command
  failures leave the source checkout unchanged;
- Claude executes in the isolated worktree while the configured checkout stays
  unchanged;
- start returns before completion and status transitions are observable;
- terminal results are bounded and omit absolute paths and raw session IDs;
- same-project jobs and Markdown writes share the modifying lock;
- independent projects can progress independently;
- cancellation terminates a queued/running task and preserves candidate files;
- resume accepts only a terminal bridge job with a captured session, reuses its
  worktree, and passes that session exactly once;
- foreign-project, unknown, active, missing-session, missing-worktree, dirty
  source, and moved-HEAD resume attempts fail closed;
- self-managed `claude_edit` is allowed, direct self-managed
  `markdown_write` remains rejected, and successful runs invoke candidate
  validation;
- candidate health uses a distinct localhost port, verifies the expected MCP
  tools, terminates the candidate process, and reports bounded failure details;
- FastMCP schemas, defaults/enums/patterns, and safety annotations match the
  contracts above.

Core tests use temporary Git repositories and fake Claude/candidate processes.
They require no ngrok, Ollama, Claude, network, or real user repository.

## Acceptance criteria

- The full prior and current core suite passes.
- Rejected operations prove source and retained worktrees unchanged where
  applicable.
- Production entry points compile and import.
- FastMCP discovery exposes the expected lifecycle schemas and annotations.
- Wheel and sdist build; a fresh wheel installation imports version `0.4.0`
  and passes `pip check`.
- Source and archives contain no real `.env`, credentials, certificates,
  `projects.json`, virtual environments, caches, worktrees, or stale build
  output.
- Live connector, ngrok, Ollama, Claude, real-repository, and self-update
  candidate checks are reported separately and never fabricated.

## Explicitly deferred

- Fixed project verification commands and verification fingerprints
- Staging and commits
- Candidate promotion, production overwrite, restart, merge, or rollback switch
- Push, tags, branches, and pull requests
- Worktree deletion through MCP
- Arbitrary shell commands, arbitrary session IDs, unrestricted Bash, or
  `bypassPermissions`
