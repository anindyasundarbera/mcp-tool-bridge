# Security model

The bridge exists to put a **governed boundary** between a remote assistant
("please do this work") and a local machine that can edit files, run git, and
push. This document is the non-negotiable boundary list and the reasoning
behind it. Enforcement lives in server code (`server.py`, `models.py`,
`registry.py`, the managers, `store.py`); it never relies on model
instructions.

For the threat model context, see [ARCHITECTURE.md §13](ARCHITECTURE.md).
For the capability gates behind each rule, see [CAPABILITIES.md](CAPABILITIES.md).

---

## Non-negotiable boundaries

These hold for every tool, every milestone, and every project configuration.
A milestone that needs to relax one requires an explicit, recorded decision.

1. **OpenAI connector mTLS protects the public ngrok endpoint.** Expose only
   the MCP server port; never expose the configured Claude launcher or the local
   filesystem directly.
2. **The local project registry (`projects.json`) is the only authorization
   source of truth.** Unknown, disabled, malformed, or unauthorized projects
   fail closed. Enforce authorization and validation in server code; do not
   rely on model instructions for security.
3. **Tool callers provide stable project IDs and relative paths** — never
   arbitrary filesystem roots, worktree paths, raw Claude session IDs,
   executables, argv, environment variables, working directories, or
   timeouts.
4. **Reject hidden paths, protected paths, traversal, and symlink escapes.**
   Hidden paths require a matching explicit hidden `commit_roots` entry; a
   broad `.` root does not authorize them.
5. **Claude Code may use `acceptEdits` only within its fixed project worktree.**
   Claude Bash, web, and secondary MCP tools remain disabled; slash commands
   and `bypassPermissions` are never enabled.
6. **Never enable `bypassPermissions` or unrestricted Bash.** Every external
   command uses a fixed argument vector with `shell=True` forbidden.
7. **No tool may silently commit, push, merge, tag, delete, restart
   production, or broaden its own permissions.** The only push surface is the
   opt-in `push_development_commit` capability: an explicit, human-approved,
   non-silent push of one verified detached commit to a new non-trunk branch
   on `origin` — never a force-push, merge, tag, trunk update, or ref move.
8. **The opt-in `runtime_verification` capability is evidence-only.** It runs
   one policy-named check against the canonical checkout and never grants
   commit authority. The MCP caller may supply only `project_id`, `check_id`,
   and `expected_head` — never an executable, argv, environment, working
   directory, timeout, or shell text. There is no `run_command`/`exec`/
   `shell`/`bash` surface. Commands execute directly with
   `asyncio.create_subprocess_exec` (no shell) and the bridge's full process
   environment is **not** inherited.
9. **The opt-in `working_tree_candidate_capture` capability is read-only
   against the source checkout.** It never stages, resets, restores, checks
   out, cleans, or writes any canonical file, and never alters index state,
   Git configuration, remotes, branches, or `.reasonix` content. The
   include/protected-exclude scope is pinned in trusted policy, not
   caller-chosen; protected exclusions always override inclusions.
10. **The running bridge must not directly edit its production checkout.** Its
    own changes require the candidate-worktree workflow (MCPB-004).
11. **`.env`, tokens, certificates, credentials, virtual environments, caches,
    and local registry secrets must never enter Git or milestone archives.**
    `projects.json`, `.env`, `.venv/`, `openai-connectors-mtls-ca.pem`, and
    `__pycache__/` are gitignored.

---

## The two mutation surfaces (and everything they refuse)

The canonical checkout at `project.root` is read-only by default. Two opt-in
capabilities are the **only** ways the bridge mutates shared git state:

### `push_development_commit` — the only push surface
- publishes exactly the bridge-created detached commit;
- to a brand-new `refs/heads/<branch>` on `origin` via
  `<commit_sha>:refs/heads/<branch>` with **no force flag**;
- rejects the repository default branch, `main`, and `master`;
- requires the remote branch not to exist;
- confirms the commit belongs to the retained job and re-derives the
  fingerprint from the commit object;
- requires a used verification record for the exact evidence;
- one branch per job, with idempotent retry;
- **never** force-pushes, merges, tags, deletes, moves another ref, updates a
  trunk, or creates a PR.

### `sync_canonical_checkout` — the only local-checkout update surface
- fast-forwards the guarded canonical checkout to exactly the current tip of
  `origin`'s default branch via `git merge --ff-only`;
- requires a clean checkout on the repository default branch;
- requires `target_commit == refs/remotes/origin/<default>` after a fetch;
- requires the caller to repeat the exact current HEAD and target commit;
- refuses while a non-terminal job exists; warns about completed-uncommitted
  jobs that lose their verify/commit path after the base moves;
- **never** pushes, forces, creates a merge commit, tags, deletes, or moves a
  remote ref.

Everything else that mutates state operates only on a detached job worktree or
the bridge-owned `.state` database.

---

## Why each boundary exists

- **mTLS at the edge + registry authorization** — the public endpoint is
  reachable by anyone with the URL; mTLS ensures only the paired ChatGPT
  connector can connect, and the registry ensures only allowlisted projects
  and capabilities are usable even then. Defense in depth.
- **No caller-supplied executables/argv/env/cwd/timeout** — a remote caller
  that could supply an executable could run anything as the bridge's user.
  Fixed argv vectors and the forbidden interpreter/loader env keys
  (`PYTHONPATH`, `LD_PRELOAD`, …) close the injection surface that a
  "run this command" tool would open.
- **Separate human approvals + exact-match confirmations** — replay and
  confusion attacks are blocked because commit/push/sync each re-derive the
  fingerprint and require the caller to repeat the exact prior state.
  A stale or mismatched confirmation fails closed.
- **Per-project locks + single-use IDs** — serializes modifying operations per
  project (a write can't overlap a job) while independent projects proceed
  concurrently; single-use verification/review IDs are consumed atomically
  (`WHERE used = 0`), so two concurrent consumers cannot both win.
- **Detached worktrees, clean-baseline checks, `validate_resume`** — the
  production checkout is never edited in place; a job's `base_head` is pinned
  and rechecked before verify/commit, so a checkout that moved under a job
  fails rather than committing against the wrong base.
- **Stale-base guard + `prepare_canonical_for_work`** — new work does not
  silently branch off a stale `HEAD` after a remote merge; the orchestrator
  fast-forwards the canonical checkout through a bridge tool instead of a
  human running `git pull`.
- **Prompt-injection defense** — opt-in `READER_INJECTION_DEFENSE` fences +
  delimits untrusted repo content the reader ingests so model directives
  cannot ride in file contents; repo-controlled verification scripts run
  sandboxed (M-CC-11); a severity-gated supply-chain scan can reject commits
  (e.g. `grype --fail-on=high`).
- **Durable, honest state** — WAL + `synchronous=FULL` + synchronous
  re-hydration mean a crash never silently loses a job; honest reconciliation
  (in-flight → `failed`/`paused_interrupted`) means the bridge never claims a
  dead task is still running.
- **Additive-only schema, fail-closed load** — `SCHEMA_VERSION` stays 1; a
  database newer than the code is refused on open; unparseable rows are
  skipped so one corrupt record never blocks startup.

---

## What "evidence-only" means

`runtime_verification` and the read-only introspection capabilities
(`job_introspection`, `policy_introspection`, `canonical_history_read`)
produce **evidence**, never authority. Concretely: a successful runtime check
never stages, commits, pushes, merges, tags, opens a PR, moves a ref,
modifies policy, or enables a capability. Its `rtv_`-prefixed evidence IDs live
in a separate namespace from candidate `verification_id`s, so a runtime ID
cannot be replayed to authorize a commit. Introspection tools only observe
governed state; they never mutate it.

---

## Operational hardening checklist

- Keep `NGROK_AUTHTOKEN` only in `.env`; rotate it if exposed.
- Keep `projects.json` `0o600` and review it before every startup.
- Keep `JOB_WORKTREE_ROOT` outside every configured project root.
- Run only one bridge process per `JOB_WORKTREE_ROOT`.
- Put the mTLS CA bundle in `openai-connectors-mtls-ca.pem` (gitignored) and
  confirm `-----BEGIN CERTIFICATE-----` is present.
- Periodically clean orphaned worktrees:
  `git -C <project> worktree remove --force <path>`.
- After a bridge code change, run the core test suite with no network/model
  dependency before restart:
  `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests`.