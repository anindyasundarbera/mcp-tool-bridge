# MCP tool reference

The bridge exposes **32 MCP tools** via FastMCP. Every tool takes an exact
`project_id` (and an opaque `job_id` where relevant); none ever accepts a
filesystem root, worktree path, raw session ID, executable, argv, environment,
working directory, or timeout. Capability gates are enforced in `server.py`
through `active_registry.resolve(project_id, ProjectCapability.<X>)`.

`ToolAnnotations` use FastMCP's `readOnlyHint` / `destructiveHint` /
`idempotentHint` / `openWorldHint`. Tools marked *dormant* are off by default;
a project opts in by listing the matching capability in `projects.json`.

Job IDs are 32-hex (`^[0-9a-f]{32}$`); HEADs are 40-hex; fingerprints are
64-hex SHA-256.

---

## Project & repository read — `repository_read`

### `list_allowed_projects`
List the project IDs this bridge is authorized to access.
- **Params**: none
- **Annotations**: read-only, idempotent, closed-world
- **Returns**: `{ "projects": [ { project_id, display_name, description, capabilities, default_effort, allow_ultracode, self_managed } ] }`

### `get_project_status`
Get Git branch, HEAD, cleanliness, and diff summary for one project before
planning or delegating work.
- **Params**: `project_id`
- **Annotations**: read-only, not idempotent, closed-world
- **Returns**: `{ project_id, project_name, repository: { branch, head, clean, ... } }`

### `list_project_markdown`
List Markdown files beneath a project directory without reading contents.
- **Params**: `project_id`, `path` (default `"."`), `max_depth` (0–20, default 3), `max_results` (≥1, default 100)
- **Annotations**: read-only, idempotent, closed-world

### `search_project_markdown`
Literal, case-insensitive text search across Markdown files. Never runs a
shell command (fixed `rg` argv).
- **Params**: `project_id`, `query` (1–500 chars), `path` (default `"."`), `max_results` (≥1, default 30)
- **Annotations**: read-only, idempotent, closed-world

### `read_project_markdown`
Read one complete UTF-8 Markdown file with SHA-256, size, line count,
timestamp, and Git status.
- **Params**: `project_id`, `path`
- **Annotations**: read-only, idempotent, closed-world

### `read_project_file_range`
Read a bounded line range from one Markdown file. Supply a prior SHA-256 when
consistency across reads matters.
- **Params**: `project_id`, `path`, `start_line` (≥1), `end_line` (≥1), `expected_sha256?`
- **Annotations**: read-only, idempotent, closed-world

### `get_markdown_outline`
Return ATX and setext headings with levels and line numbers, excluding
headings inside fenced code blocks.
- **Params**: `project_id`, `path`
- **Annotations**: read-only, idempotent, closed-world

### `get_project_file_metadata`
SHA-256, byte size, line count, modified timestamp, and Git status for one
Markdown file, without its content.
- **Params**: `project_id`, `path`
- **Annotations**: read-only, idempotent, closed-world

---

## Markdown write — `markdown_write`

### `write_project_markdown`
Create or update one UTF-8 Markdown file beneath an approved write root.
Create never overwrites. Update requires the current SHA-256 and fails if the
file changed. Parent directories must already exist.
- **Params**: `project_id`, `path`, `content`, `write_mode` (`"create"` | `"update"`, default `"create"`), `expected_sha256?` (required for update, forbidden for create; 64-hex)
- **Annotations**: **destructive**, not idempotent, closed-world
- **Held under** the per-project modifying lock.

---

## Development lifecycle — `claude_edit`

### `send_to_local_dev`
Start an asynchronous Claude Code job in a detached Git worktree. Returns an
opaque job ID immediately. Use `high` for routine work; `ultracode` only for
complex architecture, difficult reasoning, migrations, or substantial
cross-module implementation. The configured checkout is never edited directly;
Bash, web, and secondary MCP tools are disabled.
- **Params**: `project_id`, `text` (≥1 char, the full work order), `effort` (`"high"` | `"ultracode"`, default `"high"`), `requested_base?` (40-hex; when set, must be the current tip of `origin`'s default branch after a fetch — branches the job off that fetched remote tip instead of local HEAD; omit for offline/local-only projects)
- **Annotations**: **destructive**, not idempotent, open-world
- **Refuses** when the canonical checkout is behind `origin/<default>` and no `requested_base` is pinned (stale-base guard).

### `get_development_job`
Get the current status and bounded result of one job. Never exposes the
worktree path or raw Claude session ID.
- **Params**: `project_id`, `job_id` (32-hex)
- **Annotations**: read-only, not idempotent, closed-world
- **Returns** a structured `_snapshot`: `prohibited_actions`, full `budget`, `checkpoint_count`, `milestones`, `blockers`, `pause_reason`, latest checkpoint fingerprint / changed paths.

### `resume_development_job`
Continue a terminal or `paused_*` job in its retained worktree using only the
Claude session ID previously captured by this bridge. Callers provide a job ID
and never a raw session ID. Keeps the cumulative budget; seeds the drafter
from the last checkpoint's changed-path set.
- **Params**: `project_id`, `job_id`, `text` (≥1 char), `effort` (default `"high"`)
- **Annotations**: **destructive**, not idempotent, open-world
- **Rejects** non-resumable states (`failed_infrastructure`, `quarantined_provenance`).

### `cancel_development_job`
Cancel one queued or running job and terminate its process group. Candidate
files and the worktree are retained for inspection.
- **Params**: `project_id`, `job_id`
- **Annotations**: **destructive**, idempotent, closed-world

---

## Candidate review — `candidate_review` (requires `claude_edit`)

### `list_candidate_changes`
List a completed job's retained candidate changed paths with bounded metadata
(size, SHA-256, executable flag, or deleted). Reads only the changed-path set.
- **Params**: `project_id`, `job_id`
- **Annotations**: read-only, idempotent, closed-world

### `read_candidate_file`
Read one file from a retained candidate, scoped strictly to the candidate's
changed-path set. Supports an optional bounded line range and SHA-256
consistency check. Deletions are reported without content.
- **Params**: `project_id`, `job_id`, `path` (a changed path), `start_line?`, `end_line?`, `expected_sha256?`
- **Annotations**: read-only, idempotent, closed-world

### `record_candidate_review`
Record a reviewer's structured verdict bound to the exact current candidate
state. The verdict is a non-empty JSON object. The resulting `review_id` is
single-use and may be required by `commit_verified_development_job` for
`requires_review` projects.
- **Params**: `project_id`, `job_id`, `verdict` (non-empty object), `reviewer_id?` (≤200 printable chars)
- **Annotations**: not destructive, not idempotent, closed-world

### `run_independent_review`
Spawn a fresh, read-only Claude reviewer (distinct session, never resumed
from the drafting session; only Read, Glob, Grep) over a completed candidate.
Requires the project to allow a bridge-spawned reviewer
(`allow_bridge_reviewer`). Returns a single-use `review_id` bound to the
candidate state, attested as reviewer_id `"bridge-reviewer"`.
- **Params**: `project_id`, `job_id`, `effort` (default `"high"`)
- **Annotations**: not destructive, not idempotent, open-world

### `get_candidate_review`
Retrieve a previously recorded review verdict by its single-use `review_id`,
without re-running a reviewer.
- **Params**: `project_id`, `job_id`, `review_id` (32-hex)
- **Annotations**: read-only, idempotent, closed-world

---

## Verification & commit — `verification_commit` (requires `claude_edit`)

### `verify_development_job`
Run only the fixed verification commands configured for a completed retained
job. Success binds structured command evidence to the exact base HEAD,
changed paths, and candidate fingerprint. The caller cannot supply a command,
argument, environment, or path.
- **Params**: `project_id`, `job_id`, `expected_head` (40-hex; the retained job's base HEAD)
- **Annotations**: **destructive**, not idempotent, open-world
- **Returns** a single-use `verification_id`, the ordered changed paths, and the fingerprint.

### `commit_verified_development_job`
After separate human approval, create one detached commit from the exact
unchanged state that passed `verify_development_job`. Never updates a branch,
pushes, merges, tags, promotes, or restarts production. For `requires_review`
projects, a matching single-use `review_id` is also required and consumed.
- **Params**: `project_id`, `job_id`, `verification_id` (32-hex), `expected_head` (40-hex), `expected_diff_fingerprint` (64-hex), `paths` (1–500 ordered paths), `message` (≥1 char), `review_id?` (32-hex; required for `requires_review`)
- **Annotations**: **destructive**, not idempotent, closed-world
- **Mechanics**: recheck candidate → `git add -- <paths>` (never `git add .`) → prove index matches verification → disable hooks + signing → commit on detached HEAD.

---

## Publication & sync

### `push_detached_development_commit` — `push_development_commit` (requires `verification_commit`)
After separate human approval, publish the exact detached commit created by
`commit_verified_development_job` to one **new** remote branch on `origin`.
Rejects the repository default branch, `main`, and `master`; requires the
remote branch not to exist; never force-pushes, merges, tags, or moves another
ref. Confirms the commit belongs to the retained job and realizes the verified
candidate fingerprint. One branch per job; retrying the same commit + branch
is idempotent.
- **Params**: `project_id`, `job_id`, `commit_sha` (40-hex), `branch_name` (1–200 chars; not a full ref, HEAD, default, main, or master), `expected_base_head` (40-hex), `expected_diff_fingerprint` (64-hex)
- **Annotations**: **destructive**, idempotent, open-world

### `sync_canonical_checkout` — `sync_canonical_checkout` (requires `repository_read`)
After a PR merges on the remote default branch, fast-forward one project's
guarded canonical checkout to the verified current tip of `origin`'s default
branch. Requires the exact current HEAD (the stale commit) and the target
commit. The checkout must be clean and on the repository default branch; the
target must equal `origin/<default>` after a fetch. Updates the local default
branch by fast-forward only; never pushes, forces, merges, tags, deletes, or
moves a remote ref. Refuses while a non-terminal job exists; warns about
completed-uncommitted jobs that lose their verify/commit path after the base
moves.
- **Params**: `project_id`, `expected_current_head` (40-hex), `target_commit` (40-hex)
- **Annotations**: **destructive**, idempotent, open-world

### `prepare_canonical_for_work` — `sync_canonical_checkout`
Pre-work canonical-checkout update check. Fetches `origin` and reports whether
the checkout is `up_to_date` / `updates_available` / `ahead` / `diverged` /
`dirty` / `no_origin` / `fetch_failed` — **without merging**.
- `pull=false` (default): fetch and report. When `updates_available`:
- `pull=true`: fast-forward the canonical checkout to the auto-discovered
  verified remote tip (no SHA arguments required). Reuses `sync_canonical_checkout`'s
  safety: clean checkout, on the default branch, fast-forward only. A `diverged`
  state cannot be fast-forwarded and still requires a human steward. Refuses
  while a non-terminal job exists.
- **Params**: `project_id`, `pull` (default `false`)
- **Annotations**: **destructive**, idempotent, open-world

> **Orchestrator pre-work convention**: after a PR has been raised (and
> especially after one has merged) and before `send_to_local_dev` for the next
> piece of work, call `prepare_canonical_for_work(pull=false)`. If the verdict
> is `updates_available`, call `prepare_canonical_for_work(pull=true)` to
> fast-forward, then proceed.

---

## Evidence & PR

### `export_evidence_bundle` — `export_evidence_bundle` (requires `repository_read`)
Assemble a single redacted, content-addressed evidence bundle for one job:
candidate fingerprint + changed paths + diff summary, verification records +
bindings, review records + verdict hashes, checkpoint summary, and a
bundle-wide SHA-256. Never exposes raw worktree paths, secrets, or
credentials. Read-only.
- **Params**: `project_id`, `job_id`, `expected_candidate_fingerprint` (64-hex)
- **Annotations**: read-only, idempotent, closed-world

### `open_pull_request` — `open_pull_request` (requires `push_development_commit`)
Open or update a draft PR for a job's published remote branch. Discovers an
existing PR for the branch and updates it, or opens a new draft PR. Confirms
the remote branch and PR state. Never merges. Requires the job to have been
published.
- **Params**: `project_id`, `job_id`, `base_head` (40-hex), `fingerprint` (64-hex), `title` (1–200 chars), `draft` (default `true`)
- **Annotations**: **destructive**, idempotent, open-world

---

## Introspection (dormant) — M-AL-01

### `list_development_jobs` — `job_introspection` (requires `claude_edit`)
Enumerate a project's durable development jobs without a job ID. Bounded,
sanitized list (no worktree path or session ID) with per-job governance state
(verification/review/publication presence).
- **Params**: `project_id`, `scope` (`"all"` | `"active"` | `"terminal"`, default `"all"`), `limit` (≥0, default 50, hard cap 200)
- **Annotations**: read-only, idempotent, closed-world

### `get_effective_project_policy` — `policy_introspection` (requires `repository_read`)
Expose the validated effective policy as a deterministic, redacted view with a
`policy_fingerprint`. The absolute root is never returned.
- **Params**: `project_id`
- **Annotations**: read-only, idempotent, closed-world

### `resolve_candidate_canonicalization` — `canonical_history_read` (requires `repository_read`)
Reconcile a detached development commit against canonical history. Given a
40-hex commit SHA, returns a three-state verdict:
`CANONICAL` (absorbed into `origin`'s default branch by direct ancestry or
squash/rebase content-equivalence), `NOT_CANONICAL` (provenance-linked and
published but not yet absorbed), or `INDETERMINATE` (provenance missing,
content mismatch, or canonical tip unreadable). Read-only and origin-only;
never fetches, pushes, merges, tags, or moves a ref.
- **Params**: `project_id`, `commit_sha` (40-hex)
- **Annotations**: read-only, idempotent, open-world

---

## Host runtime verification (dormant) — MCPB-011

### `run_runtime_verification` — `runtime_verification` (requires `repository_read`)
Run one named, policy-authorized runtime check against the project's clean
canonical checkout at an exact expected HEAD and return content-addressed
PASS/FAIL evidence. The caller supplies only `project_id` / `check_id` /
`expected_head`; the executable, argv, environment, working directory, and
timeout are all fixed in local policy and can never be supplied by the caller.
No shell is involved. A successful check produces evidence only — it never
stages, commits, pushes, merges, tags, opens a PR, moves a ref, or grants
commit authority.
- **Params**: `project_id`, `check_id` (`^[a-z][a-z0-9_-]{0,63}$`, a lookup key never executed), `expected_head` (40-hex)
- **Annotations**: not destructive, not idempotent, open-world
- **Mechanics**: `asyncio.create_subprocess_exec` (no shell), `start_new_session=True`, minimal deterministic env + policy overrides (full process env not inherited); read-only git only; clean + exact-HEAD + default-branch binding; repository immutability rechecked post-execution (any drift → `failed`, no auto-restore); `rtv_`-prefixed evidence IDs in a separate namespace from candidate verification IDs.

---

## Working-tree candidate capture (dormant) — MCPB-012

### `preflight_working_tree_capture` — `working_tree_candidate_capture` (requires `repository_read`)
Read-only preflight. Binds the exact canonical HEAD, resolves the approved
capture scope against the current dirty working tree, and returns the ordered
manifest, the protected-exclusion manifest, and a content-addressed
working-tree fingerprint — without creating a worktree or retaining a
candidate. The caller's `include_paths` / `protected_exclude_paths` must
equal (as sets) the scope pinned in trusted local policy (exposed by
`get_effective_project_policy` under `working_tree_capture_scope`); the
caller cannot widen, narrow, or guess the scope.
- **Params**: `project_id`, `include_paths`, `protected_exclude_paths`, `expected_base_head?` (40-hex), `expected_working_tree_fingerprint?` (64-hex)
- **Annotations**: read-only, idempotent, closed-world

### `working_tree_candidate_capture` — `working_tree_candidate_capture`
Capture an approved set of uncommitted working-tree changes from the project's
dirty canonical checkout into a retained candidate **without modifying the
canonical checkout**. Read-only against the source: never stages, resets,
restores, checks out, cleans, or writes any canonical file, and never alters
the index, Git configuration, remotes, branches, or `.reasonix` content. A
detached candidate worktree is created at the exact bound HEAD and overlaid
with only the selected, fingerprinted changes; the source working-tree
fingerprint is recomputed before success and any drift fails the capture and
leaves no usable candidate. The retained candidate is a normal governed
candidate eligible for verify/review/commit/publish.
- **Params**: `project_id`, `include_paths`, `protected_exclude_paths`, `expected_base_head?` (40-hex), `expected_working_tree_fingerprint?` (64-hex), `effort` (`"high"` | `"ultracode"`, default `"high"`)
- **Annotations**: not destructive, not idempotent, closed-world

---

## Disposable runtime action (dormant)

### `standalone_openhands_action` — `governed_disposable_runtime_action_execution`
Execute a restricted OpenHands action in an isolated disposable runtime. The
process is governed: binds to a canonical baseline, restricts tools to
TerminalTool and FileEditorTool, and binds the final result to an OH-001D
ledger.
- **Params**: `project_id`, `action_params` (object including `canonical_baseline_fingerprint`, `action_specification`, `expected_outcome`)
- **Annotations**: **destructive**, not idempotent, open-world

---

## What no tool can ever do

- silently commit, push, merge, tag, delete, restart production, or broaden its
  own permissions;
- accept an executable, argv, environment, working directory, or timeout from a
  caller;
- edit the production checkout directly (modifying work happens in detached
  worktrees; the only canonical-checkout mutation is opt-in `sync_canonical_checkout`,
  fast-forward only);
- expose absolute project roots, raw worktree paths, raw session IDs, secrets,
  or raw environment data in responses;
- run a command with `shell=True`.

The one push surface is opt-in `push_development_commit` (non-trunk, origin-only,
no-force). The one local-checkout update surface is opt-in `sync_canonical_checkout`
(fast-forward only to `origin/<default>`).