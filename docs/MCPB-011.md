# MCPB-011 — Governed host runtime verification

## Decision

BridgeLab verifies candidate work only inside isolated Claude development
worktrees (`verify_development_job` → `commit_verified_development_job`). Some
checks — integration tests that need the Docker daemon, loopback networking,
host-managed services, or integration-test infrastructure — cannot run
meaningfully in that isolated worktree. MCPB-011 adds a **new, narrow, opt-in
capability** `runtime_verification` that executes one **policy-named** runtime
check against the project's **canonical checkout** and returns
content-addressed PASS/FAIL evidence. It is **dormant by default**: a project
that does not opt in observes no new tool and no broadened permission.

```
run_runtime_verification(project_id, check_id, expected_head)
```

The caller supplies only `project_id`, `check_id` (selecting a check defined in
trusted local policy), and `expected_head` (a 40-hex SHA the canonical checkout
must be at). The executable, argv, environment, working directory, and timeout
are all **fixed in local policy** and can never be supplied by the caller. No
shell is involved. A successful check produces **evidence only** — it never
stages, commits, pushes, merges, tags, opens a PR, moves a ref, modifies
policy, enables a capability, or grants commit authority.

## Threat model — why this is not a generic host shell

The defining security property is that **the MCP caller can never choose the
executable, the arguments, the environment, the working directory, the
timeout, or any shell text**. The MCP contract exposes exactly three
parameters — `project_id`, `check_id`, `expected_head` — and nothing else:

| Field | Caller-supplied? | Source |
|---|---|---|
| `project_id` | yes (exact ID only) | registry lookup |
| `check_id` | yes (pattern-anchored lookup key, never executed) | policy dict-key |
| `expected_head` | yes (40-hex SHA only) | git HEAD comparison |
| executable / argv | **no** | `RuntimeVerificationCommand.argv` in `projects.json` |
| environment | **no** | `RuntimeVerificationCommand.environment` in `projects.json` |
| working directory | **no** | always `project.root` |
| timeout | **no** | `RuntimeVerificationCommand.timeout_seconds` or the global bound |
| shell | **no** | never; `asyncio.create_subprocess_exec`, no `shell=True`/`bash -c` |

Concretely:

- `check_id` is validated by the schema pattern `^[a-z][a-z0-9_-]{0,63}$` before
  it reaches the manager, so shell metacharacters, whitespace, uppercase, and
  traversal strings are rejected at the schema boundary. It is used **only as a
  dict-key lookup** against `runtime_verification_commands`; a value that
  matches no configured check fails closed before any subprocess.
- The configured `argv` is a fixed 1–64-argument vector validated at policy load
  (non-empty, NUL-free, ≤2000 chars). A shell executable (`sh`, `bash`, `dash`,
  `zsh`, `ksh`, `fish`, `powershell`, `pwsh`, `cmd`) is rejected, so no
  configured command can ever become a shell surface. `{python}` is allowed
  only as `argv[0]` and is expanded to `sys.executable` at run time.
- The configured `environment` is a small validated mapping (≤64 entries; keys
  are uppercase `[A-Z_][A-Z0-9_]*`; interpreter/loader injection keys —
  `PYTHONPATH`, `PYTHONHOME`, `LD_PRELOAD`, `LD_LIBRARY_PATH`, `DYLD_*`,
  `SHLIB_PATH`, `PERL5LIB`, … — are rejected). `PATH` is allowed so a
  host-runtime check can discover tools like `docker`.
- The process is spawned with `asyncio.create_subprocess_exec` (no shell),
  `start_new_session=True`, `stdin=DEVNULL`, in a new process group so a
  timeout kills the whole group (`SIGTERM` → `SIGKILL`), exactly like the
  existing `_run_exec`.
- The bridge's **full process environment is not inherited** — only a small
  deterministic base (`LANG`/`LC_ALL`=`C.UTF-8`, `NO_COLOR=1`,
  `PYTHONIOENCODING=utf-8`, `PYTHONDONTWRITEBYTECODE=1`, `TZ=UTC`, plus `HOME`
  and `PATH` from the bridge env so host tools and per-user config resolve)
  plus the policy `environment` overrides. No token, certificate, or secret
  leaks from the bridge into the check.

This is deliberately **not** a remote command executor, terminal MCP,
deployment engine, Docker API, SSH executor, CI system, or job scheduler. It
is one narrowly-scoped, evidence-only capability.

## Canonical binding and repository immutability

Runtime verification runs against the **canonical checkout**, not a Claude
worktree, and reuses the model proven in `SyncManager.sync`:

- **Read-only git only** before execution: `git branch --show-current`,
  `git rev-parse --verify HEAD`, `git status --porcelain=v1
  --untracked-files=all`. The bridge never fetches, pulls, merges, resets,
  checks out, switches, stashes, or cleans before a runtime check.
- **Binding** requires the working tree to be **clean** and `HEAD == expected_head`.
  Where BridgeLab knows the default branch (via `refs/remotes/origin/HEAD`),
  the checkout must be **on that branch**; for local-only repos with no
  origin/HEAD the branch check is skipped (fail open, per the spec's "where
  BridgeLab already knows the default branch"). Any binding failure fails
  closed **before any subprocess is spawned**.
- **Immutability** is captured before and revalidated after execution. If
  `HEAD`, the working tree, or the branch changed during the check, the run
  is `status="failed"` with `repository.mutated=true` — **no auto-restore, no
  PASS**. Repository mutation invalidates the evidence.

## Evidence model

A run returns a content-addressed evidence object:

- `runtime_verification_id` — `"rtv_" + 32 hex`. The `rtv_` prefix puts IDs in
  a **separate namespace** from the candidate `verification_id` pattern
  (`^[0-9a-f]{32}$`), so a runtime ID **structurally cannot** be fed to
  `commit_verified_development_job` and never creates commit authority.
- `policy_fingerprint` — SHA-256 over the sorted JSON of the project's
  `runtime_verification_commands`. A hash, so it leaks no environment values;
  it lets a caller detect any change to the effective policy that authorized a
  check.
- `evidence_fingerprint` — deterministic SHA-256 over
  `{project_id, check_id, verified_head, policy_fingerprint, status, exit_code,
  stdout_hash, stderr_hash, repository_unchanged}` (timestamps excluded) so
  the same outcome at the same HEAD reproduces the same fingerprint and a
  different outcome yields a different one.
- `stdout` / `stderr` — bounded, redacted dicts `{text, bytes, truncated,
  sha256}`; top-level `stdout_hash` / `stderr_hash` mirror them. The SHA-256
  is computed over the **final redacted, bounded** text, never the raw output,
  so the hash cannot act as a confirmation oracle for a secret that was
  redacted.
- `repository` — `{pre_head, pre_branch, pre_clean, post_head, post_branch,
  post_clean, unchanged, mutated}`.
- `status` (`passed` / `failed`), `executed` (false for binding failures),
  `exit_code`, `started_at`, `completed_at`, `timeout_seconds`, `error`.

Redaction reuses BridgeLab's existing `_redact` (absolute project roots,
worktree root, and config path → `<redacted-path>`), and additionally redacts
policy environment values of length ≥ 12 (plausible secrets/host paths) from
captured stdout/stderr. Short flag values (`host`, `1`, …) are left intact so
legitimate test output is not mangled. The absolute project root never appears
in evidence.

## Runtime vs candidate verification

| | Candidate verification | Runtime verification |
|---|---|---|
| Tool | `verify_development_job` | `run_runtime_verification` |
| Runs against | isolated candidate worktree | canonical checkout |
| Host runtime | no (hermetic worktree) | yes (Docker, loopback, host services) |
| Caller selects | `job_id`, `expected_head` | `check_id`, `expected_head` |
| Command source | `verification_commands` (policy) | `runtime_verification_commands` (policy) |
| Authority | success → single-use `verification_id` → can commit | **evidence only**, no commit authority |
| ID namespace | `^[0-9a-f]{32}$` | `rtv_<32 hex>` (separate) |
| Mutates repo | no (reads candidate) | no (read-only git; mutation → FAIL) |

## Capability and policy model

`runtime_verification` (requires `repository_read`, like
`sync_canonical_checkout` and `export_evidence_bundle`) is opt-in. A project
listing the capability must configure at least one `RuntimeVerificationCommand`
(fail closed; an empty capability would be misleading authority), and runtime
check IDs must be unique per project. Existing projects are unaffected: the
field defaults to empty and the capability is not granted.

`get_effective_project_policy` exposes the configured runtime checks
**sanitized**: check IDs, `execution_mode`, the (path-redacted) `argv`,
`has_environment`, `timeout_seconds`, and the environment **keys** with every
value replaced by `<redacted>` — so a caller can see which env vars a check
consumes without learning any secret or host path.

## Durable store

One additive `runtime_verifications` table in `bridge.db`, created with
`CREATE TABLE IF NOT EXISTS`; `SCHEMA_VERSION` stays 1 and existing databases
upgrade in place. Both PASS and bounded FAIL rows are persisted (append-only
audit ledger keyed by `runtime_verification_id`), mirroring the `syncs` audit
table, and re-hydrated on bridge restart. Only bounded, redacted data is
stored.

## Files changed

| Path | Purpose |
|---|---|
| `src/local_dev_mcp/models.py` | `RUNTIME_VERIFICATION` capability + `RuntimeVerificationCommand` + shared argv validator + policy validation |
| `src/local_dev_mcp/config.py` | `RUNTIME_VERIFICATION_COMMAND_TIMEOUT_SECONDS` + `MAX_RUNTIME_VERIFICATION_OUTPUT_BYTES` bounds |
| `src/local_dev_mcp/runtime_verification.py` | **new** — `RuntimeVerificationManager`: canonical binding, no-shell execution, evidence, fingerprints, persistence |
| `src/local_dev_mcp/store.py` | `runtime_verifications` table + insert/load |
| `src/local_dev_mcp/server.py` | wire manager + `run_runtime_verification` MCP tool |
| `src/local_dev_mcp/introspection.py` | sanitized `runtime_verification_commands` in effective policy |
| `tests/test_runtime_verification.py` | **new** — full coverage |
| `tests/test_server.py`, `tests/test_config.py`, `tests/test_checkpoints.py`, `tests/test_persistence.py` | tool-count canaries (28→29) + policy-validation + schema assertions |

## Operational requirement

The bridge must be **restarted** (or reconstructed via `create_server`) before
`run_runtime_verification` becomes visible: FastMCP registers tools once at
server-construction time. No existing project gains the capability without an
explicit `projects.json` change. Live Docker/integration smoke is
host-dependent and not asserted by the unit suite, which uses self-contained
`{python}` scripts so it runs without Docker.