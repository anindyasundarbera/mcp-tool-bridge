# MCPB-005 — Deterministic verification and approved commit

## Decision

A project may opt in to `verification_commit` by configuring exact verification
argv vectors and approved commit roots in the local project registry. MCP
callers choose neither commands nor authorization roots.

`verify_development_job` operates only on a completed retained MCPB-004 job. It
requires the clean source checkout and detached worktree to remain at the job's
base HEAD, rejects a pre-staged index and unsafe paths, runs every configured
command without a shell in a minimal environment, and succeeds only when all
commands pass without changing the candidate. The result binds a single-use
verification ID to the base HEAD, an exact ordered path list, structured command
evidence, and a staging-invariant SHA-256 file-state fingerprint.

The MCPB-004 candidate health probe now also requires both MCPB-005 tool names
before a self-managed candidate can be reported healthy.

`commit_verified_development_job` is a separate human-confirmed destructive
operation. It requires the exact verification ID, HEAD, fingerprint, path list,
and commit message; rechecks the state; stages only `git add -- <paths>`; proves
the index blobs reproduce the verified fingerprint; disables hooks and signing;
and creates one clean detached commit with the verified base as its parent.

## Security boundary

- No caller-supplied command, argument vector, environment, working directory,
  timeout, commit root, or absolute path
- No shell execution, unrestricted Bash, Git hooks, or commit signing
- No commit of a failed, changed, replayed, foreign, unauthorized, or
  pre-staged candidate
- No branch/ref update, push, merge, tag, promotion, restart, rollback switch,
  deletion, or worktree cleanup
- Verification and commit use the existing per-project modifying lock
- Source checkouts remain unchanged; commits advance only the retained detached
  worktree HEAD

## Configuration

| Setting | Default |
|---|---:|
| `VERIFICATION_COMMAND_TIMEOUT_SECONDS` | `600` |
| `MAX_VERIFICATION_OUTPUT_BYTES` | `1048576` |
| `MAX_COMMIT_MESSAGE_CHARS` | `500` |

Project command records have a stable `id`, a bounded `argv`, and optional
relative `python_path` directories. `{python}` is accepted only as the first
argv element and resolves to the running bridge interpreter.
