# M-CC-09 — The hermetic verification lab

## Decision

BridgeLab verifies a development candidate by running the project's configured
`verification_commands` against an isolated candidate worktree. That baseline
is deterministic in **time** (`TZ=UTC`, `SOURCE_DATE_EPOCH=0`) and **hash**
(`PYTHONHASHSEED=0`), and M-CC-09 (tail) makes the *environment the commands
run in* deterministic and constrained, opt-in per project, without adding any
new MCP tool:

- **PIN_TOOLCHAIN** — `toolchain_pin`: an absolute, operator-pinned interpreter
  (a venv directory or an executable) that replaces `{python}` for the
  project's verification commands, so the commands run against a pinned runtime
  instead of the bridge's own interpreter. A SHA-256 **toolchain-pin digest**
  (over the canonicalized pin path, never the path itself) is folded into
  `compute_environment_digest` and reported as `toolchain_pin_digest`, so a
  verification's environment fingerprint changes only when the operator changes
  the pin.
- **DEPS_CACHE_DIR** — `deps_cache_dir`: an absolute, read-only dependency
  cache. A sandboxed verification command is exposed to it as the
  `DEPS_CACHE_DIR` env var and a **read-only bind** inside the sandbox; a
  non-sandboxed command cannot guarantee read-only, so it **fails closed**
  before running.
- **NETWORK_POLICY** (`none` | `allowlist`) — enforced through the existing
  bubblewrap sandbox. `none` runs the command with **no network namespace**
  (`--unshare-net`) and **fails closed** when the command is not sandboxed.
  `allowlist` binds a generated hosts file (every allowlisted host pinned to
  127.0.0.1 loopback) plus an **empty** `/etc/resolv.conf` into the sandbox, so
  DNS for any non-allowlisted host fails closed — a sandboxed command can only
  reach the allowlisted loopback endpoints (a project's local proxy). It too
  **fails closed** when the command is not sandboxed (a DNS allowlist is not
  enforceable outside the sandbox).
- **baseline_comparison** — confirmed working and now positively tested: a
  candidate whose fingerprint equals the last green verification's
  `baseline_fingerprint` is reported `baseline_matches: true`; drift is flagged.

All fields are **opt-in** (`None` / empty tuple by default), so existing
projects and the existing 441-test suite are untouched until a project opts in.
The hermetic lab introduces **no new tool**: the 32-tool canary surface is
unchanged. Fail-closed cases return a failed command result (no exit code, no
authority), which fails the whole verification without granting any commit
authority.

## Surface

New optional `ProjectPolicy` fields (validated at policy load):

```json
{
  "toolchain_pin": "/opt/venvs/project-pinned",
  "deps_cache_dir": "/opt/deps-cache/project",
  "network_policy": "allowlist",
  "network_allowlist": ["cache.internal", "pypi.internal"]
}
```

- `toolchain_pin` / `deps_cache_dir` must be **absolute**, free of `..`, and
  are canonicalized with `Path.resolve()` (empty string → unset). The
  toolchain-pin digest is stable across runs because the canonicalized path is
  what is hashed.
- `network_allowlist` entries are lowercase hostnames (`[a-z0-9.-]`, 1-253
  chars, no empty/`-`-bounded label), de-duplicated. `network_policy`:
  - `"allowlist"` **requires** a non-empty `network_allowlist`;
  - `"none"` **rejects** a configured `network_allowlist`;
  - `None` **rejects** a configured `network_allowlist`.
  Inconsistency fails closed at policy load.

Verification response gains two additive, path-free fields:
`toolchain_pin_digest` and `network_policy`.

## Enforcement semantics

| Config | Sandboxed command | Non-sandboxed command |
|---|---|---|
| `toolchain_pin` | `{python}` → pinned interpreter | same |
| `deps_cache_dir` | `DEPS_CACHE_DIR` env + `--ro-bind` (read-only) | **fails closed** |
| `network_policy: none` | `--unshare-net` (no network namespace) | **fails closed** |
| `network_policy: allowlist` | hosts file (loopback pin) + empty resolv.conf | **fails closed** |

The fail-closed result is `{"status": "failed", "exit_code": None, "error":
<reason>}` for the offending command, which fails the whole verification closed.
`_sandbox_argv` stays a pure builder; the caller (`_run_command`) owns the two
generated temp files (`_write_network_hosts_file`, `_write_empty_resolv_conf`)
and unlinks them in a `finally`. The milestone-acceptance path in `jobs.py`
passes `project=` through the same `_run_command`, so the hermetic lab applies
to milestone acceptance commands too. (The earlier `/dev/null`-at-resolv.conf
experiment produced a "Permission denied" on read inside the sandbox; an empty
regular temp file gives the same fail-closed DNS with clean semantics.)

## Security model (preserved, not widened)

- No `shell=True`; fixed argument vectors; the pinned interpreter replaces only
  a literal `{python}` at `argv[0]`.
- `deps_cache_dir` is bound **read-only** in the sandbox — a repo-controlled
  script can never write to it.
- The allowlist hosts file and empty resolv.conf are generated temp files
  (mode 0600) bound read-only; DNS for non-allowlisted hosts fails closed.
- Nothing new is pushed, pulled, merged, tagged, or opened; no new tool, no new
  caller-supplied path beyond the validated opt-in policy paths.
- All new fields are operator-pinned trusted-local policy — never caller input.

## Tests

`tests/test_verification_lab.py` now has **16 tests** (baseline 9 + 7 new), and
`tests/test_verification.py`'s fake runner accepts the new `project=` kwarg
(signature-only). New coverage:

- `test_baseline_matches_on_identical_candidate_repeat` — the same candidate
  re-verified after a green run reports `baseline_matches: true` (positive
  baseline path).
- `test_pinned_toolchain_is_used` — a fake pinned venv's interpreter runs the
  command (`PINNED-TOOLCHAIN` marker on stdout).
- `test_network_policy_none_fails_closed_for_unsandboxed_command` /
  `test_network_policy_none_sandboxed_command_is_network_isolated` —
  `--unshare-net` asserted on the sandbox argv; non-sandboxed fails closed.
- `test_network_policy_allowlist_pins_dns_to_allowlisted_hosts` — generated
  hosts file pins `cache.internal` to 127.0.0.1, no `--unshare-net`, empty
  resolv.conf bound at `/etc/resolv.conf`.
- `test_deps_cache_dir_exposed_read_only_for_sandboxed_command` /
  `test_deps_cache_dir_fails_closed_for_unsandboxed_command` —
  `DEPS_CACHE_DIR` env + `--ro-bind` asserted; non-sandboxed fails closed.
- `VerificationLabPolicyValidationTests` (5) — allowlist-requires-non-empty;
  `none` rejects an allowlist; allowlist without `network_policy` rejected;
  `toolchain_pin` / `deps_cache_dir` must be absolute.

## Status

Implemented and verified locally. Full suite passes (448 tests, 441 baseline +
7 new), including the 32-tool count canaries in `test_server.py`,
`test_checkpoints.py`, and `test_persistence.py` (unchanged — no new tool).
Version bumped to 0.10.2 in `pyproject.toml`.

**NOT COMMITTED, NOT PUSHED, NO PR OPENED, LIVE BRIDGELAB NOT RESTARTED.**
Commit, push, live installation/restart, and any real-repository smoke test
require separate Human Steward authorization after review.
