# Capabilities & project configuration

The bridge authorizes every action through a **capability model**: each project
in `projects.json` lists a `frozenset` of `ProjectCapability` values, and every
MCP tool resolves its `project_id` against a required capability before doing
anything. Capabilities are validated at startup, so an invalid or
self-contradictory `projects.json` fails the server before it can serve a
request.

This document describes the 15 capabilities, their dependency graph, the
`projects.json` schema, and the opt-in discipline (dormant-by-default) that
governs every new surface.

---

## The 15 capabilities

Defined in `src/local_dev_mcp/models.py` (`ProjectCapability`, a `StrEnum`).

| Capability | Tool(s) it gates | Requires | Dormant? |
|---|---|---|---|
| `repository_read` | all project/repo-read tools; prerequisite for several below | — | base |
| `markdown_write` | `write_project_markdown` | — | base |
| `claude_edit` | `send_to_local_dev`, `get_development_job`, `resume_development_job`, `cancel_development_job` | — | base |
| `verification_commit` | `verify_development_job`, `commit_verified_development_job` | `claude_edit` | base |
| `candidate_review` | `list_candidate_changes`, `read_candidate_file`, `record_candidate_review`, `run_independent_review`, `get_candidate_review` | `claude_edit` | base |
| `push_development_commit` | `push_detached_development_commit` | `verification_commit` | opt-in |
| `sync_canonical_checkout` | `sync_canonical_checkout`, `prepare_canonical_for_work` | `repository_read` | opt-in |
| `export_evidence_bundle` | `export_evidence_bundle` | `repository_read` | opt-in |
| `open_pull_request` | `open_pull_request` | `push_development_commit` | opt-in |
| `job_introspection` | `list_development_jobs` | `claude_edit` | dormant (M-AL-01) |
| `policy_introspection` | `get_effective_project_policy` | `repository_read` | dormant (M-AL-01) |
| `canonical_history_read` | `resolve_candidate_canonicalization` | `repository_read` | dormant (M-AL-01) |
| `runtime_verification` | `run_runtime_verification` | `repository_read` + ≥1 `runtime_verification_commands` | dormant (MCPB-011) |
| `working_tree_candidate_capture` | `preflight_working_tree_capture`, `working_tree_candidate_capture` | `repository_read` + pinned `working_tree_capture_scope` | dormant (MCPB-012) |
| `governed_disposable_runtime_action_execution` | `standalone_openhands_action` | — | dormant (OH) |

"Base" means a project may list the capability directly; "opt-in" and
"dormant" mean the same thing in practice — **off by default**; a project
opts in only by explicitly listing the capability (and satisfying its
prerequisites) in `projects.json`.

---

## Capability dependency graph

```
repository_read ─┬─ markdown_write (independent)
                 ├─ claude_edit ─┬─ verification_commit ─┬─ push_development_commit ─ open_pull_request
                 │               │                       └─ candidate_review
                 │               └─ candidate_review
                 ├─ sync_canonical_checkout
                 ├─ export_evidence_bundle
                 ├─ job_introspection        (M-AL-01, also requires claude_edit)
                 ├─ policy_introspection
                 ├─ canonical_history_read
                 ├─ runtime_verification     (MCPB-011, + ≥1 runtime check)
                 └─ working_tree_candidate_capture (MCPB-012, + pinned scope)

governed_disposable_runtime_action_execution  (standalone, OH ledger)
```

These dependencies are enforced in `ProjectPolicy.validate_policy`
(`models.py`). The validator raises at startup if a project lists a capability
without its prerequisite, or lists a capability that requires a configured
object it does not have (e.g. `verification_commit` without ≥1
`verification_commands`, or `runtime_verification` without ≥1
`runtime_verification_commands`, or `working_tree_candidate_capture` without a
pinned `working_tree_capture_scope`). A capability with nothing to select is
misleading authority, so the validator fail-closes.

### Concrete dependency rules (verbatim from the validator)
- `candidate_review` requires `claude_edit`.
- `verification_commit` requires `claude_edit` + a non-empty `commit_roots` + ≥1 `verification_commands`.
- `push_development_commit` requires `verification_commit`.
- `open_pull_request` requires `push_development_commit`.
- `sync_canonical_checkout`, `export_evidence_bundle`, `policy_introspection`,
  `canonical_history_read`, `runtime_verification`,
  `working_tree_candidate_capture` all require `repository_read`.
- `runtime_verification` additionally requires ≥1 `runtime_verification_commands`.
- `working_tree_candidate_capture` additionally requires a pinned
  `working_tree_capture_scope` (non-empty `include_paths`).
- `job_introspection` requires `claude_edit`.
- `markdown_write` requires ≥1 `markdown_write_roots`.
- `self_managed` projects **cannot** also grant `markdown_write` against the
  production checkout (development must go through worktrees).
- `requires_review` requires both `verification_commit` and `candidate_review`
  (otherwise no review can be recorded and the commit gate deadlocks).
- `allow_bridge_reviewer` requires `candidate_review`.
- `default_effort: "ultracode"` is rejected when `allow_ultracode` is false.
- Verification, supply-chain, and runtime-verification command IDs must be
  unique per project.

---

## `projects.json` schema

Top level (a `ProjectRegistryDocument`, Pydantic, `extra="forbid"`):

```json
{
  "version": 1,
  "projects": [ <ProjectPolicy>, ... ]
}
```

`version` is fixed at `1`. `projects` is non-empty; IDs must be unique.

### `ProjectPolicy` fields

| Field | Type | Notes |
|---|---|---|
| `id` | string `^[a-z][a-z0-9-]{1,63}$` | stable project ID used by every tool call |
| `display_name` | string 1–120 | human label |
| `description` | string 1–500 | human description |
| `root` | absolute path | the canonical checkout; must be the git top level when `require_git` |
| `enabled` | bool (default true) | disabled projects are skipped at load |
| `require_git` | bool (default true) | require `root` to be a git repo top level |
| `capabilities` | list of `ProjectCapability` | the authorization surface |
| `capability_definitions` | map (default empty) | detailed per-capability operation registration |
| `markdown_write_roots` | list of relative paths | required when `markdown_write` is granted |
| `commit_roots` | list of relative paths | required when `verification_commit` is granted; `.` does not authorize hidden paths |
| `protected_paths` | list of relative paths (default `[".git",".env"]`) | never readable/writable/committable |
| `verification_commands` | list of `VerificationCommand` | fixed argv; see below |
| `supply_chain_scans` | list of `SupplyChainCommand` | severity-gated commit-time scans |
| `runtime_verification_commands` | list of `RuntimeVerificationCommand` | host-runtime checks |
| `working_tree_capture_scope` | `WorkingTreeCaptureScope` or null | pinned include/protected-exclude scope |
| `default_effort` | `"high"` \| `"ultracode"` or null (default null) | per-project default; null inherits the global `CLAUDE_DEFAULT_EFFORT` |
| `allow_ultracode` | bool (default true) | must be true if `default_effort` is `ultracode`; a global `ultracode` default is rejected at job start when false |
| `self_managed` | bool (default false) | enables candidate self-test; forbids `markdown_write` |
| `requires_review` | bool (default false) | commit requires a consumed `review_id` |
| `allow_bridge_reviewer` | bool (default false) | enables `run_independent_review` |
| `claude_model` | string or null | override the global `CLAUDE_MODEL` |
| `claude_max_turns` | int 1–200 or null | override the global `CLAUDE_MAX_TURNS` |

### Command models

All command models validate `argv` with `_validate_command_argv`:
- a list of 1–64 non-empty, NUL-free, ≤2000-char arguments;
- `{python}` is allowed **only** as `argv[0]` and expands to `sys.executable`;
- a shell executable (`sh`/`bash`/`dash`/`zsh`/`ksh`/`fish`/`powershell`/`pwsh`/`cmd`) is rejected — no configured command can ever become a shell surface.

**`VerificationCommand`** — runs against an isolated candidate worktree at
verify time:
```json
{
  "id": "core-tests",
  "argv": ["{python}", "-m", "unittest", "discover", "-s", "tests", "-v"],
  "python_path": ["src"],
  "sandbox": false
}
```
`id` is `^[a-z][a-z0-9_-]{0,63}$`; `python_path` is ≤16 relative dirs → confined `PYTHONPATH`; `sandbox` (M-CC-11) runs repo-controlled verification scripts hermetically.

**`SupplyChainCommand`** — runs at commit time; a non-zero exit rejects the
commit (exactly how `grype --fail-on=high` surfaces a finding):
```json
{ "id": "grype-high", "argv": ["grype", "dir:.", "--fail-on=high"] }
```

**`RuntimeVerificationCommand`** — runs against the canonical checkout with
host-runtime access (Docker, loopback, host services):
```json
{
  "id": "integration-runtime",
  "argv": ["{python}", "-m", "unittest", "discover", "-s", "tests"],
  "execution_mode": "host",
  "timeout_seconds": 600,
  "environment": { "PATH": "/usr/local/bin:/usr/bin:/bin" }
}
```
`environment` keys must be uppercase `^[A-Z_][A-Z0-9_]*` (≤64 entries, values ≤4000 chars, NUL-free). **Forbidden keys** (interpreter/loader injection vectors): `PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, `LD_PRELOAD`, `LD_LIBRARY_PATH`, `LD_AUDIT`, `DYLD_INSERT_LIBRARIES`, `DYLD_LIBRARY_PATH`, `DYLD_FALLBACK_LIBRARY_PATH`, `SHLIB_PATH`, `LIBPATH`, `PERL5LIB`, `PERL5OPT`, `RUBYOPT`. `PATH` is intentionally allowed so a check can discover tools like `docker`. The bridge never inherits its full process environment anyway.

**`WorkingTreeCaptureScope`** — pinned include/protected-exclude scope:
```json
{
  "include_paths": ["AGENTS.md", "docs/**"],
  "protected_exclude_paths": [".reasonix/desktop-topic-titles.json"]
}
```
Paths are relative, non-empty, no `..`, no empty/`.` segments. `**` is allowed only as the final segment (`docs/**`), not mid-path. Protected exclusions always override inclusions and never appear in the captured candidate.

---

## A worked example

See `projects.example.json` for three projects: a read+write knowledge store, a
self-managed `local-dev-mcp` (the bridge itself, with verification, introspection,
and runtime verification), and a disabled `research-workspace` demonstrating
`working_tree_candidate_capture` with protected-exclude scope. A minimal
verification-and-commit project looks like:

```json
{
  "id": "my-project",
  "display_name": "My Project",
  "description": "An allowlisted git repository",
  "root": "/abs/path/to/repo",
  "enabled": true,
  "require_git": true,
  "capabilities": [
    "repository_read",
    "claude_edit",
    "verification_commit",
    "push_development_commit"
  ],
  "commit_roots": ["src", "tests"],
  "protected_paths": [".git", ".env"],
  "verification_commands": [
    { "id": "pytest", "argv": ["{python}", "-m", "pytest", "-q"], "python_path": ["src"] }
  ],
  "default_effort": "high",
  "allow_ultracode": true,
  "self_managed": false
}
```

---

## Opt-in discipline (non-negotiable)

Every milestone that adds a **new** tool or capability must:

1. add a `ProjectCapability` value (requiring its dependency capability);
2. register the tool in `server.py` with explicit FastMCP `ToolAnnotations`;
3. ship **dormant by default** — a project opts in only by listing the
   capability in `projects.json`;
4. keep the schema additive (`CREATE TABLE IF NOT EXISTS`, `SCHEMA_VERSION`
   stays 1, no `ALTER TABLE` without a sidecar table) when it needs durable
   state;
5. bump the tool-count canaries in `tests/test_server.py`,
   `tests/test_checkpoints.py`, and `tests/test_persistence.py` (they assert the
   exact tool count);
6. bump the package version in `pyproject.toml`;
7. record a decision (`docs/MCPB-0NN.md`) and implementation brief
   (`docs/MCPB-0NN-IMPLEMENTATION-BRIEF.md`).

A capability is never auto-granted. An opt-in capability must never widen an
existing boundary beyond what the project already authorizes — introspection
capabilities, for example, are gated on the prerequisite mutation/read
capability they observe, so a project cannot introspect a surface it is not
already authorized to use.