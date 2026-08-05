# MCPB-005 — Deterministic verification and approved commit

## Objective

Complete the reviewed-candidate workflow introduced by MCPB-004. A completed
development job may be verified in its retained detached worktree by exact
commands configured in the local project registry. A separate, destructive
tool may then create one detached commit only when the worktree is byte-for-byte
the state that passed verification and the caller repeats the exact base HEAD,
diff fingerprint, changed paths, and approved commit message.

The package version for this milestone is `0.5.0`.

MCPB-005 does not promote the detached commit, update a branch or other ref,
push, merge, tag, restart production, switch a checkout, delete a worktree, or
expose an unrestricted shell.

## Project policy

Projects opt in with the `verification_commit` capability. That capability
requires both:

- `verification_commands`: one or more registry-defined command records with a
  stable ID and a non-empty argv vector; and
- `commit_roots`: one or more project-relative roots beneath which every
  changed path must fall.

The MCP caller never supplies an executable, argument, environment variable,
working directory, timeout, or path root. The bridge invokes configured argv
directly with `create_subprocess_exec`, never through a shell. The special first
argument `{python}` resolves to the Python interpreter running the bridge.
Every command runs from the retained worktree with a minimal environment and a
single global timeout. An optional registry-defined `python_path` contains only
relative, symlink-free directories inside the worktree and becomes the command's
`PYTHONPATH`.

Commit roots are authorization boundaries, not discovery hints. Hidden paths
are allowed only when an explicitly configured commit root covers them, while
the project's `protected_paths` remain forbidden. Changed symbolic links and
paths traversing symbolic-link components are rejected.

## Tool contracts

### Verify a completed job

`verify_development_job(project_id, job_id, expected_head)`:

1. resolves the exact project with `verification_commit` authority;
2. acquires the shared per-project modifying lock;
3. requires a completed, inactive job with a retained detached worktree;
4. requires the source checkout to remain clean at the job's base HEAD;
5. requires the worktree HEAD to equal both the job base HEAD and the caller's
   `expected_head`, with no pre-staged changes;
6. discovers every tracked and untracked changed path without rename folding;
7. requires every path to be normalized, protected-path-safe, symlink-free,
   and beneath a configured commit root;
8. computes a SHA-256 state fingerprint from the exact base HEAD, full binary
   tracked diff, and the path/mode/content of every untracked file;
9. runs all configured verification commands in order, capturing bounded
   stdout/stderr evidence and hashes; and
10. recomputes the state and succeeds only when every command passes and the
    worktree fingerprint and changed paths are unchanged.

The response contains a bridge-generated 32-character verification ID, base
HEAD, diff fingerprint, exact changed paths, verification timestamp, and one
structured result per configured command. It exposes no absolute paths or
environment values. A failed or state-changing verification creates no commit
authority.

### Commit the verified state

`commit_verified_development_job(project_id, job_id, verification_id,
expected_head, expected_diff_fingerprint, paths, message)` is a separately
confirmed destructive operation. It:

1. resolves the same project and job and acquires the same modifying lock;
2. requires an unused successful verification record for that exact project
   and job;
3. requires the caller's HEAD, fingerprint, and ordered unique path list to
   match the verification evidence exactly;
4. recomputes the worktree state and rejects any drift;
5. requires an empty index, then stages only the exact verified paths with
   `git add -- <paths>`;
6. proves the staged set is exact, the working tree has no unstaged or
   untracked candidate changes, and the staged state still matches the
   verification fingerprint;
7. commits with Git hooks and commit signing disabled; and
8. proves the new detached commit has the verified base HEAD as its sole parent,
   contains exactly the authorized paths, and leaves the worktree clean.

The verification record is single-use. If staging or commit fails, the bridge
attempts to restore the index to the verified base HEAD while preserving
working-tree files, and returns an error. A successful response includes the
new detached commit SHA, its parent, committed paths, and message. It never
updates a branch or remote ref.

## Fingerprint format

The fingerprint is SHA-256 over a staging-invariant, versioned,
length-delimited byte stream:

- literal format marker;
- exact 40-character base HEAD;
- for each sorted changed path: UTF-8 path bytes, exact HEAD tree entry, and
  either a deletion marker or the current Git file mode, byte length, SHA-256,
  and exact file bytes.

After exact staging, the bridge independently reconstructs the same manifest
from index blobs and requires the fingerprint to match. This detects Git clean
filters or other staging transformations and makes staged versus unstaged
placement irrelevant to the bound content.

Ignored files are not candidate changes and are neither fingerprinted nor
staged. The bridge rejects invalid UTF-8 paths, non-regular untracked files,
symlinks, staged changes before verification, an empty change set, and command
state drift.

## Configuration

Add and document:

```dotenv
VERIFICATION_COMMAND_TIMEOUT_SECONDS=600
MAX_VERIFICATION_OUTPUT_BYTES=1048576
MAX_COMMIT_MESSAGE_CHARS=500
```

All limits are positive. Command IDs, argv size, argv element length,
`python_path`, commit roots, and commit messages are schema-bounded.

## MCP metadata

- `verify_development_job`: read-write, destructive, non-idempotent,
  open-world. Verification commands may execute project code, whose behavior
  can include external interaction, and may create ignored artifacts, although
  successful verification requires the candidate state to remain unchanged.
- `commit_verified_development_job`: read-write, destructive, non-idempotent,
  closed-world.

All job and verification IDs use exact 32-character lowercase hexadecimal
schemas. HEAD and fingerprint values use exact 40- and 64-character lowercase
hexadecimal schemas. The commit path list is non-empty and bounded; the server,
not only the schema, enforces normalization, uniqueness, exact equality, and
policy authorization.

## Required tests

Keep all 51 MCPB-001 through MCPB-004 tests passing and add coverage for:

- registry validation and public metadata for the new capability;
- fixed argv execution, `{python}` resolution, minimal environment, ordered
  results, output bounds, timeout/process-group termination, and no shell;
- stable fingerprints for tracked edits, additions, deletions, executable-mode
  changes, binary files, and untracked files;
- rejection of empty, staged, protected, outside-root, symlinked, non-regular,
  invalid-path, moved-HEAD, dirty-source, foreign-project, unknown-job,
  non-completed-job, and state-changing verification attempts;
- failed commands produce structured evidence but no usable verification ID;
- commit requires the exact unused verification, HEAD, fingerprint, path list,
  and message bounds;
- exact `git add -- <paths>` behavior, no hook execution, no branch/ref update,
  exact parent and committed paths, and a clean detached worktree;
- any drift, extra staged path, duplicate path, path reordering, replay,
  staging failure, or Git failure is rejected without an unintended commit;
- verification and commit share the project modifying lock with writers and
  development jobs, while independent projects remain independent; and
- FastMCP schemas and safety annotations match this brief.

Core tests use temporary repositories and fixed fake verification commands.
They require no Claude, Ollama, ngrok, network, or real user repository.

## Acceptance criteria

- The complete prior and current core suite passes.
- Production entry points compile and import.
- FastMCP discovery exposes exactly the intended 15 tools with the schemas and
  annotations above.
- Wheel and sdist build; a fresh wheel installation imports version `0.5.0`
  and passes `pip check`.
- Source and archives contain no real `.env`, credentials, certificates,
  project registry, virtual environments, caches, worktrees, or stale build
  output.
- A complete final source ZIP is produced with an exact file inventory and
  SHA-256.
- Live connector, Claude, candidate, and real-repository smoke checks remain
  separate and are never fabricated.

## Explicitly excluded

- Caller-supplied commands, shell strings, arbitrary environment variables, or
  unrestricted Bash
- Push, pull request creation, branch/ref update, merge, tag, rebase, cherry-pick,
  candidate promotion, production overwrite/restart, or rollback switching
- Commit of an unverified or changed state
- Commit hooks, signing, deletion, and worktree cleanup through MCP
