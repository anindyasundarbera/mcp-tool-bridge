# MCPB-012 — Governed working-tree candidate capture

## Decision

BridgeLab develops candidates inside isolated Claude worktrees produced from a
clean canonical checkout. A known case exists — first raised for `mrbera` —
where the only approved changes live as **uncommitted edits in a dirty
canonical working tree** (canonical doc changes at HEAD are absent; a future
directory migration must include `AGENTS.md` and `docs/**`; two `.reasonix/`
files are local state that must never be captured). The existing pipeline
cannot retain those changes as a governed candidate without first committing
them to the canonical checkout, which the safety model forbids.

MCPB-012 adds a **new, narrow, opt-in capability** `working_tree_candidate_capture`
that captures an approved set of uncommitted working-tree changes into a
retained BridgeLab candidate **without modifying the canonical checkout**. The
capture is **read-only against the source**: it never stages, resets, restores,
checks out, cleans, renames, deletes, or writes any canonical working-tree
file, and it never alters index state, Git configuration, remotes, branches, or
`.reasonix` content. The captured candidate is a normal retained governed
candidate, eligible unchanged for `verify_development_job`, `candidate_review`,
`verification_commit`, and `push_development_commit`.

It is **dormant by default**: a project that does not opt in observes no new
tool and no broadened permission. Opt-in requires `repository_read` and a
**scope pinned in trusted local policy**.

```
preflight_working_tree_capture(project_id, include_paths, protected_exclude_paths,
                               expected_base_head?, expected_working_tree_fingerprint?)
working_tree_candidate_capture(project_id, include_paths, protected_exclude_paths,
                               expected_base_head?, expected_working_tree_fingerprint?,
                               effort?)
```

## Two-step preflight/capture contract

| Field | Caller-supplied? | Source |
|---|---|---|
| `project_id` | yes (exact ID only) | registry lookup |
| `include_paths` | yes (must equal policy scope as a set) | echoed + checked against `working_tree_capture_scope.include_paths` |
| `protected_exclude_paths` | yes (must equal policy scope as a set) | echoed + checked against `working_tree_capture_scope.protected_exclude_paths` |
| `expected_base_head` | optional (40-hex SHA from preflight) | git HEAD comparison |
| `expected_working_tree_fingerprint` | optional (64-hex from preflight) | fingerprint comparison |
| the approved scope itself | **no** | `WorkingTreeCaptureScope` in `projects.json` |
| worktree location / base commit | **no** | derived from canonical HEAD |
| overlay bytes | **no** | read from the canonical working tree |
| effort | yes (`high`/`ultracode`) | recorded on the retained job |

The caller **must** supply `include_paths` and `protected_exclude_paths` that
equal — as sets — the scope pinned in trusted local policy (exposed by
`get_effective_project_policy` under `working_tree_capture_scope`). The caller
cannot widen, narrow, or guess the scope: a mismatch fails closed before the
canonical checkout is read. The recommended flow is **preflight then capture**:
preflight binds HEAD, resolves the scope against the current dirty tree, and
returns the ordered manifest and a working-tree fingerprint; capture echoes the
returned `base_head` and `working_tree_fingerprint` so it can prove it acted on
the exact state the preflight observed. If the caller omits the expected
provenance, the response still returns the discovered values and the
implementation enforces an internally consistent capture (pre- and
post-revalidation against disk).

## Safety invariants

1. **Never modify the canonical checkout.** The capture runs only read-only
   Git against the source (`rev-parse`, `status`, `ls-tree`, `worktree add
   --detach`) and read-only filesystem reads of the selected working-tree
   files. The candidate is materialised in a **separate detached worktree** at
   the exact bound HEAD; the source working tree, index, refs, config, and
   `.reasonix` content are never written.
2. **Bind capture to exact provenance.** The exact canonical HEAD SHA is
   recorded and re-checked. A working-tree fingerprint is computed before the
   source is read, recomputed after source collection, and recomputed once
   more immediately before success. Any drift — HEAD moved, a selected or
   protected-excluded file changed, the changed-path set changed — fails the
   capture and leaves **no usable candidate**.
3. **Require explicit, approved scope.** The caller supplies the include and
   protected-exclude path sets, which must equal the policy-pinned scope.
   Protected exclusions always override inclusions and never appear in the
   candidate manifest. Paths must be non-empty, relative, free of `..`, and
   use `**` only as a trailing full segment; absolute, traversal, duplicate,
   and overlapping-ambiguous scopes are rejected.
4. **Capture only declared scope.** The candidate is materialised from the
   exact HEAD baseline and overlaid with only the selected, fingerprinted
   changes. Unrelated dirty files are never silently included. Deletions are
   represented as deletions. Changed symbolic links and submodules are
   rejected (verification rejects them too).
5. **Locks are not trusted alone.** The per-project lock serialises BridgeLab
   work, but external (non-BridgeLab) edits remain possible, so every capture
   re-validates source state from disk before succeeding.
6. **Retained-candidate lifecycle.** A successful capture registers a
   development job directly in the `COMPLETED` / inactive / uncommitted state
   (never running Claude) via `register_captured_candidate`, so every
   downstream gate accepts it unchanged and the candidate is restart-safe. A
   failed capture force-removes the worktree and persists a `rejected` audit
   row; no partial candidate survives.

## Working-tree fingerprint algorithm

The fingerprint is a canonical SHA-256 over a versioned marker plus the
canonical JSON (`sort_keys=True`, UTF-8) of:

```json
{
  "head": "<lowercase 40-hex base_head>",
  "include_paths": ["<sorted include patterns>"],
  "protected_exclude_paths": ["<sorted protected-exclude patterns>"],
  "selected": [<per-path descriptor for each selected changed path, path-sorted>],
  "protected_excluded": [<per-path descriptor for each protected changed path, path-sorted>]
}
```

Each per-path descriptor is:

```json
{
  "path": "<relative posix path>",
  "kind": "added" | "modified" | "deleted",
  "staged": <bool>,        // index changed relative to HEAD
  "unstaged": <bool>,      // working tree changed relative to index
  "mode": "<git mode: 100644 | 100755 | baseline mode for deletions>",
  "size": <int | null>,    // null for deletions
  "content_sha256": "<64-hex | null>",
  "symlink_target": null   // selected symlinks are rejected, so always null
}
```

The fingerprint **distinguishes HEAD, staged vs unstaged, additions, deletions,
untracked selected paths, path, git mode/exec, content hash, and selected vs
protected-excluded scope**, in deterministic order. It contains **no
timestamps** and is **not a git-diff-text digest**. The candidate fingerprint
returned alongside it is computed by the **same** `_digest_candidate_paths`
routine that `verify_development_job` uses, so the captured candidate is
content-addressed identically at the gate boundary.

Drift detection recomputes this fingerprint from a fresh `git status` snapshot
and fresh file reads after the overlay and again immediately before success;
any mismatch (`fingerprint_pre != fingerprint_post`, HEAD moved, or any
descriptor field changed) fails the capture.

## Errors

| Condition | Outcome |
|---|---|
| project lacks the capability / scope | `WorkingTreeCaptureError`, rejected audit row, no candidate |
| caller paths ≠ policy scope (widened/narrowed) | rejected, no candidate |
| absolute / `..` / duplicate / overlapping caller or policy paths | rejected (policy paths rejected at load; caller paths at capture) |
| an include pattern selects no changed path | rejected, no candidate |
| selected path is a symlink / submodule / non-regular | rejected, no candidate |
| selected path is policy-protected / outside commit roots | rejected, no candidate |
| protected-excluded path would leak into the manifest | rejected, no candidate |
| manifest exceeds `MAX_WORKING_TREE_CAPTURE_MANIFEST_PATHS` (default 500) | rejected, no candidate |
| a file exceeds `MAX_WORKING_TREE_CAPTURE_FILE_BYTES` (default 10 MiB) | rejected, no candidate |
| `expected_base_head` / `expected_working_tree_fingerprint` mismatch | rejected, no candidate |
| HEAD moves during capture | worktree force-removed, rejected audit row, no candidate |
| selected or protected file changes during capture | worktree force-removed, rejected audit row, no candidate |
| worktree changed-path set ≠ manifest after overlay | worktree force-removed, rejected, no candidate |

Every rejection persists an append-only `working_tree_captures` audit row with
`status = "rejected"` and the error text; only a successful capture binds a
usable job (`status = "captured"`, `capture_id = job_id`).

## Lifecycle

```
preflight (read-only)
   └─ returns base_head, manifest, protected_exclude_manifest, working_tree_fingerprint
capture
   ├─ resolve scope (caller == policy)
   ├─ bind HEAD, resolve manifest, compute fingerprint_pre
   ├─ validate expected provenance (if supplied)
   ├─ create detached worktree at base_head      [read-only on source]
   ├─ overlay selected fingerprinted bytes       [read-only on source; worktree index stays empty]
   ├─ revalidate from disk: HEAD + fingerprint_post + descriptor stability
   ├─ compute candidate_fingerprint via verification's _digest_candidate_paths
   ├─ register_captured_candidate (COMPLETED/inactive/uncommitted job)
   ├─ persist captured audit row
   └─ return response
       on any failure → force-remove worktree, persist rejected audit row, raise
downstream gates (unchanged)
   verify_development_job → candidate_review → commit_verified_development_job → push_development_commit
```

## MrBera scope

Inspected directly against the MrBera working tree at HEAD
`8949ef89e218ea4964b16c4a2110150d903a50d2`. The dirty set is:

- `AGENTS.md` (M) — included
- `docs/*.md` (12 modified + 3 untracked = 15 files) — included via `docs/**`
- `.reasonix/desktop-topic-auto-title-meta.json` (M) — **protected-excluded**
- `.reasonix/desktop-topic-titles.json` (M) — **protected-excluded**

The exact two protected `.reasonix` paths were discovered from `git status`,
not guessed. The pinned scope in `projects.json`:

```json
"working_tree_capture_scope": {
  "include_paths": ["AGENTS.md", "docs/**"],
  "protected_exclude_paths": [
    ".reasonix/desktop-topic-auto-title-meta.json",
    ".reasonix/desktop-topic-titles.json"
  ]
}
```

16 selected paths (well under the 500-path limit). The capture rejects if
either protected path would be captured or its source content changes
mid-capture. This task **does not run the directory migration itself** — it
ends once the capability is implemented, verified, enabled for `mrbera`, and
ready for a separate governed job to use.

## Example successful response shape (redacted)

```json
{
  "capture_id": "<job_id>",
  "job_id": "<job_id>",
  "candidate_id": "<job_id>",
  "project_id": "mrbera",
  "base_head": "8949ef89e218ea4964b16c4a2110150d903a50d2",
  "include_paths": ["AGENTS.md", "docs/**"],
  "protected_exclude_paths": [
    ".reasonix/desktop-topic-auto-title-meta.json",
    ".reasonix/desktop-topic-titles.json"
  ],
  "manifest": [
    {"path": "AGENTS.md", "kind": "modified", "mode": "100644",
     "size": 4096, "content_sha256": "…", "symlink_target": null,
     "staged": false, "unstaged": true},
    {"path": "docs/AI_RND_FRAMEWORK.md", "kind": "modified", "mode": "100644",
     "size": 8192, "content_sha256": "…", "symlink_target": null,
     "staged": false, "unstaged": true}
  ],
  "protected_exclude_manifest": [
    {"path": ".reasonix/desktop-topic-auto-title-meta.json", "kind": "modified",
     "mode": "100644", "size": 256, "content_sha256": "…",
     "symlink_target": null, "staged": false, "unstaged": true}
  ],
  "working_tree_fingerprint": "<64-hex>",
  "candidate_fingerprint": "<64-hex>",
  "policy_fingerprint": "<64-hex>",
  "started_at": "2026-08-21T…Z",
  "captured_at": "2026-08-21T…Z",
  "status": "captured",
  "effort": "high"
}
```

## Closure report

### Files changed

**Implementation**
- `src/local_dev_mcp/models.py` — `WORKING_TREE_CANDIDATE_CAPTURE` capability;
  `WorkingTreeCaptureScope` (frozen, `extra=forbid`, path validation via
  `_capture_scope_path`, duplicate rejection, non-empty `include_paths`); scope
  field on `ProjectPolicy`; policy validation (requires `repository_read` and a
  pinned scope).
- `src/local_dev_mcp/config.py` — `max_working_tree_capture_manifest_paths`
  (default 500) and `max_working_tree_capture_file_bytes` (default 10 MiB).
- `src/local_dev_mcp/worktrees.py` — `GitWorktreeManager.create_detached_at_head`
  (detached worktree at exact HEAD without requiring a clean source; read-only
  on the canonical checkout; re-checks HEAD did not move).
- `src/local_dev_mcp/jobs.py` — `DevelopmentJobManager.new_job_id` and
  `register_captured_candidate` (registers a `COMPLETED`/inactive/uncommitted
  job with `working_tree_capture` authority records, never running Claude).
- `src/local_dev_mcp/store.py` — additive `working_tree_captures` audit table
  and `insert_working_tree_capture_row` / `load_working_tree_capture_rows`
  accessors (append-only ledger; SCHEMA_VERSION unchanged).
- `src/local_dev_mcp/working_tree_capture.py` — **new**:
  `WorkingTreeCaptureManager` with scope resolution, glob resolution, the
  working-tree fingerprint, manifest resolution, read-only overlay, two-step
  preflight/capture, drift revalidation, cleanup, and persistence. Reuses
  verification's `candidate_changed_paths`, `_digest_candidate_paths`,
  `_validate_policy_path`, `_open_candidate_read`, and `_git_bytes` so the
  candidate fingerprint matches what `verify_development_job` produces.
- `src/local_dev_mcp/server.py` — `WorkingTreeCaptureManager` wired into
  `create_server`; two MCP tools `preflight_working_tree_capture` (read-only)
  and `working_tree_candidate_capture` (non-destructive candidate-producing).
- `src/local_dev_mcp/introspection.py` — `working_tree_capture_scope` (sorted,
  sanitized) added to `effective_policy_view` and included in
  `policy_fingerprint`.

**Configuration**
- `projects.json` — `mrbera` granted `working_tree_candidate_capture` and
  pinned `working_tree_capture_scope` (include `AGENTS.md` + `docs/**`;
  protected-exclude the two discovered `.reasonix` paths).
- `projects.example.json` — new disabled `research-workspace` example
  demonstrating the capability + scope shape.

**Tests**
- `tests/test_working_tree_capture.py` — **new**: 17 capture cases (modified /
  staged-add / untracked / deletion / exec-bit / symlink rejection /
  protected-exclusion / widened / narrowed / invalid paths / overlapping
  scope / no-match pattern / HEAD-drift / selected-drift / protected-drift /
  two-step contract / gate-compatibility + source-unmodified) plus pattern &
  overlap unit tests, all over real temporary Git repositories.
- `tests/test_config.py` — 8 `WorkingTreeCapturePolicyTests` (capability
  prerequisites, required scope, scope path validation, glob-segment,
  duplicate, and string-value rejection).
- `tests/test_server.py`, `tests/test_checkpoints.py`, `tests/test_persistence.py`
  — tool-count canaries updated 29 → 31; `test_server` additionally asserts the
  two new tools exist and preflight is read-only.

### Capability / config enablement

`mrbera` is enabled for `working_tree_candidate_capture` with the pinned scope
above. `projects.json` and `projects.example.json` both load and validate,
including the capability-requires-scope and capability-requires-`repository_read`
checks.

### Tests run + results

```
.venv/bin/python -m pytest tests/ -q
→ 340 passed, 42 subtests passed
```

Includes the 17 capture cases, 8 policy tests, 3 updated canaries, and the full
existing local-dev-mcp suite (no regressions). The unrelated `tokengate/`,
`LightRAG/`, and `RAG-Anything/` subprojects that share the repo root are not
part of this server and are excluded from the run.

### Evidence the canonical checkout and `.reasonix` were not modified

`test_17_captured_candidate_is_gate_compatible_and_source_unmodified` snapshots
`git status --porcelain`, `git rev-parse HEAD`, and the bytes of
`.reasonix/a.json` before and after a capture and asserts all three are
byte-identical post-capture. It also asserts the retained worktree's index is
empty (`git diff --cached HEAD` is empty) and that the candidate fingerprint
reproduces via the verification code path.

### Limitations / follow-ups

- The capture reuses verification's symlink/submodule rejection: a selected
  path that is a symlink (or a typechange to a symlink) is refused. If MrBera
  ever needs to capture a symlink change, a separate fingerprint extension
  would be required.
- The overlay preserves the git mode (exec bit) for regular files but does not
  capture sparse-checkout / gitattributes filter changes; these are out of
  scope for the current MrBera doc migration.
- Running the actual MrBera directory migration is explicitly out of scope:
  this task ends with the capability implemented, verified, enabled, and ready
  for a separate governed job to invoke `preflight_working_tree_capture` then
  `working_tree_candidate_capture` against `mrbera`.
- Downstream publication still flows through the unchanged governed gates
  (`verify_development_job` → review → `commit_verified_development_commit` →
  `push_development_commit`); no gate was bypassed or widened.