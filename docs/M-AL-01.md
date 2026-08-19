# M-AL-01 — Read-only introspection (agent-lab Phase-0)

## Decision

The bridge can create, verify, commit, publish, review, sync, and open PRs for a
development candidate, but an orchestrator cannot **observe** the governed state
it produced without a job ID, cannot see the **validated effective policy** that
actually authorizes a project (only the raw JSON), and cannot **reconcile** a
detached candidate commit against the canonical history it was supposed to land
in. Agent-lab's Phase-0 platform definition needs all three: enumerate jobs by
project, reason about a project's real authorization surface, and answer "is
this candidate commit canonical yet?" without re-running the publication/PR/sync
machinery or mutating anything.

M-AL-01 adds three **read-only introspection MCP tools**, dormant by default and
opt-in per project through three new capabilities. They reuse the existing
managers and the existing authorization boundary — they introduce **no new
mutation path, no arbitrary repository selection, no arbitrary shell/git, no
caller-supplied remote, origin-only remote reasoning, bounded responses, no
secret leakage, no production checkout modification, no push, no merge, no tag,
no branch deletion, no ref movement, no deployment**. The milestone is named
**M-AL-01** (not MCPB-011, which the roadmap reserves for the Evidence release
M-EV-01) because it is the agent-lab Phase-0 enabler, not a numbered BridgeLab
core milestone.

## Surface

Three MCP tools (tool count 24 → 27), each gated by its own capability and
authorized through the existing `ProjectRegistry.resolve(project_id, capability)`:

- **`list_development_jobs(project_id, scope?, limit?)`** — enumerate a project's
  durable jobs without a job ID. `scope` is `all` (default) / `active` /
  `terminal`; `limit` defaults to 50 and is clamped to `MAX_LIST_JOBS = 200`. Each
  entry exposes only safe fields — `job_id`, `project_id`, `status`,
  `execution_phase` (terminal/in_flight), `effort`, `attempt`, `created_at`,
  `updated_at`, `committed`, `verification_present`, `review_present`,
  `publication_present`, and (when present) `committed_sha`, `base_head`,
  `publication_branch`. The raw worktree path, Claude session ID, result,
  candidate, error, and `cancel_requested` are never exposed (mirroring
  `stale_resumable_job_summaries`/`_snapshot`). Governance flags come from three
  bounded reads of the durable tables; PR state is in-memory and intentionally
  omitted. Capability: `job_introspection` (requires `claude_edit`).

- **`get_effective_project_policy(project_id)`** — expose the **already
  validated** effective `ProjectPolicy` (it does NOT re-parse JSON) as a
  deterministic, redacted view: sorted `capabilities`/`markdown_write_roots`/
  `commit_roots`/`protected_paths`, `verification_commands`/`supply_chain_scans`
  serialized deterministically (sorted by id), optional `claude_model`/
  `claude_max_turns`, `root_redacted = true` (the absolute root is never
  returned), and a SHA-256 `policy_fingerprint` over the canonical JSON of every
  exposed field so a caller can detect any change. Embedded absolute paths
  (e.g. a verification-command `argv` that carries `LAB_ROOT=/abs/path`) are
  redacted. Capability: `policy_introspection` (requires `repository_read`).

- **`resolve_candidate_canonicalization(project_id, commit_sha)`** — given a
  40-hex commit, reconcile the candidate against canonical history and return a
  three-state verdict. `openWorldHint = true` (it reads origin). Capability:
  `canonical_history_read` (requires `repository_read`).

## Capabilities and dependency rules

Three `ProjectCapability` values, added after `OPEN_PULL_REQUEST`:

```
JOB_INTROSPECTION = "job_introspection"
POLICY_INTROSPECTION = "policy_introspection"
CANONICAL_HISTORY_READ = "canonical_history_read"
```

Each is gated on the prerequisite capability it introspects, so a project cannot
introspect a surface it is not already authorized to use
(`models.validate_policy`):

- `job_introspection` requires `claude_edit`
- `policy_introspection` requires `repository_read`
- `canonical_history_read` requires `repository_read`

Enabled for `agent-lab` (Phase-0 requirement) and for `local-dev-mcp` (self-
managed, `self_managed = true` preserved, no `markdown_write`) so the bridge can
introspect its own governed state. `projects.example.json` updated to match.

## The four-layer evidence model

`CanonicalizationResolver.resolve` (in `introspection.py`) opens its own
read-only `bridge.db` connection (mirroring every other manager) and combines:

- **Layer 1 — provenance.** `DevelopmentJobManager.committed_job_summary` (a new
  lock-guarded, safe-fields-only accessor) finds the project's job that produced
  `commit_sha`; the durable `verifications`/`publications` rows link it to the
  verified candidate (diff_fingerprint, paths) and the published branch.
- **Layer 2 — content check.** `verification.committed_state` re-derives the
  candidate fingerprint directly from the committed git object via read-only
  `git ls-tree -z`/`cat-file` (worktrees share the canonical checkout's object
  store, so this runs from `project.root`). The re-derived fingerprint must
  equal the verification record's `diff_fingerprint`; a mismatch means the
  commit object does not realize the verified candidate.
- **Layer 3 — branch & PR.** When a publication record names a branch, the
  resolver reads the remote branch state via read-only `ls-remote`
  (`PullRequestManager._remote_branch_state`) and the associated PR via
  read-only `gh pr list`/`gh pr view` (`_discover_pr`/`view_pr`, a new read-only
  counterpart). Both degrade gracefully: a remote/gh failure is surfaced as an
  error field rather than crashing the reconciliation.
- **Layer 4 — canonical ancestry.** The origin default branch comes from the
  local `origin/HEAD` symref (`symbolic-ref`, read-only); the remote tip comes
  from `ls-remote` (read-only, **no fetch**). The live tip SHA from `ls-remote`
  is not guaranteed to be in the local object store, and a read-only tool may not
  fetch it; if the tip object is absent locally, ancestry and tree-equivalence
  against it cannot run, so Layer 4 reports a distinct `remote_tip_not_local`
  status (rather than swallowing the unresolvable `merge-base`/`log` into a false
  `not_in_history`) and the verdict becomes INDETERMINATE. Two ancestry facts are
  computed and kept distinct (never collapsed): `candidate_is_ancestor` (the
  candidate commit is reachable from the canonical tip, via
  `merge-base --is-ancestor`) and `landing_is_ancestor` (the PR merge/landing SHA
  is reachable from the canonical tip). When the landing SHA is known but absent
  from the local object store (and a fetch is forbidden), ancestry is `None` and
  `landing_present_locally` is `False` so the verdict can be INDETERMINATE rather
  than a false NOT_CANONICAL. Squash content-equivalence is tested by comparing
  the commit's tree against the most recent `_SQUASH_SEARCH_DEPTH = 100`
  first-parent commits on the canonical tip (bounded, so the tool never scans
  unbounded history).

- **Layer 2.5 — content equivalence at landing.** For squash/rebase merges the
  candidate SHA is not an ancestor even though its substantive content landed.
  The resolver compares the `(mode, blob)` state of every changed path in the
  verification record between the candidate commit and a landing commit on
  canonical history (the tree-matched commit from Layer 4, or the PR landing SHA
  when it is an ancestor) via read-only `git ls-tree -r -z`. This handles
  added/modified/deleted paths and executable-bit (mode) changes; renames are
  covered when both old and new paths are in the changed-path set. It fails
  closed: a mismatch on any path means the candidate's content did not land
  as-is, so the caller must not report CANONICAL on this evidence. This is the
  rebase path (a squash onto a moved base produces a different tree, which
  whole-tree equivalence misses but per-path equivalence catches).

## The three-state verdict

| Verdict | `canonicalization_method` | When |
|---|---|---|
| `CANONICAL` | `exact_commit_ancestor` | The candidate commit is reachable from the origin default-branch tip (direct ancestry). |
| `CANONICAL` | `merged_pr_content_equivalent` | A content-equivalent commit (same tree, squash) exists on canonical history, OR the PR is merged, its landing SHA is an ancestor of the canonical tip, and the candidate's changed-path content is equivalent at landing (rebase). |
| `NOT_CANONICAL` | `not_merged` | Provenance links the commit to the project and it is published, but it is not absorbed (PR open, PR closed-without-merge, or no PR record). |
| `NOT_CANONICAL` | `landing_not_in_canonical_history` | The PR was merged but its landing SHA is no longer an ancestor of the canonical tip (reverted / history rewritten past it). |
| `NOT_CANONICAL` | `landing_content_mismatch` | The PR landed but the candidate's changed-path content does not match the landing commit's state — the substantive content did not land as-is. |
| `INDETERMINATE` | `insufficient_evidence` | The commit object is not present locally (and a fetch is forbidden), no institutional record links it to the project, its content does not match the verified candidate, the canonical default-branch tip could not be read from origin, the canonical tip is known remotely but its object is not present locally (origin advanced past the last sync and a fetch is forbidden), the PR layer (gh) was unavailable so the merge/landing state is unknown, the PR is merged but its landing SHA could not be determined, the PR is merged but its landing SHA is unavailable locally, or the landing commit is on canonical history but its per-path content could not be read to confirm equivalence. |

The verdict is never forced into a boolean: uncertainty becomes `INDETERMINATE`.
The two ancestry facts (`candidate_is_ancestor`, `landing_is_ancestor`) are
reported as distinct fields and never collapsed.

## Response shape

The structured response carries: `project_id`, `commit_sha`, `verdict`,
`canonicalization_method`, `reason`, `warnings` (a list of soft signals such as
an unreadable canonical tip or an unavailable landing SHA), `commit_present_locally`,
`provenance` (job/verification/publication binding), `content_check` (Layer 2
fingerprint integrity: does the commit object realize the verified candidate),
`content_equivalence` (`status` = equivalent/mismatch/not_checked/unavailable,
`checked_paths`, `mismatches`, `landing_commit`), `canonical` (`default_branch`,
`current_head`, `candidate_is_ancestor`, `landing_sha`, `landing_is_ancestor`,
`landing_present_locally`, `content_equivalent`, `matched_canonical_commit`,
`status`), `remote_branch`, `pull_request` (`merge_commit_sha` is the landing
SHA), and `audit_trail`. Credentials and private filesystem paths are never
returned; every result is wrapped in `_redact`.

The audit trail comes from the append-only `syncs` table
(`SyncManager.audit`); `remote_url` is deliberately omitted (it may carry
embedded credentials). Every result is wrapped in `_redact` over the project
root, the worktree root, the projects config path, and every project root.

## Security model (preserved, not widened)

The resolver issues only read-only git (`rev-parse`, `merge-base`, `log`,
`ls-tree`, `cat-file`, `ls-remote`, `symbolic-ref`) and read-only `gh`
(`pr list`, `pr view`). It never runs `git fetch` (which mutates `.git`
object/ref state), never pushes, merges, tags, moves a ref, or modifies the
canonical checkout. It is origin-only: the remote is always the configured
`origin`; no caller-supplied remote is accepted. The `gh` runner is the
fixed-argv `_gh` (no shell, stdin `DEVNULL`). A hermetic test fixture
(`test_never_fetches_pushes_merges_or_moves_refs`) spies on every git
subprocess the resolver issues, asserts the subcommand set is a subset of the
read-only allow-list, asserts the forbidden set (`fetch`/`push`/`pull`/`merge`/
`rebase`/`tag`/`update-ref`/`reset`/`checkout`/`commit`/`rm`/`clean`/`stash`) is
empty, and asserts the canonical checkout's refs are byte-for-byte unchanged
across the reconciliation.

## Tests

`tests/test_introspection.py` (29 tests) covers: `effective_policy_view`
determinism/redaction/fingerprint; `list_development_jobs` sanitization,
governance index, scope, limit clamping, project isolation; the
`committed_job_summary` accessor; capability authorization at both the registry
and the server (`ToolError` when the capability is missing); and **twenty-one
hermetic reconciliation fixtures** spanning the verdict matrix, the structured
`canonicalization_method`, the distinct `landing_is_ancestor` fact, per-path
content equivalence at landing, the INDETERMINATE edge cases, and the read-only
invariant. The reconciliation fixtures reuse the hermetic bare-origin pattern
from `test_sync` and drive a real job through verify → commit → publish so the
detached commit, verification record, and publication record are genuine; `gh`
is mocked (no real GitHub). The fixtures cover: direct ancestry (CANONICAL);
squash content-equivalence (CANONICAL); published-not-merged (NOT_CANONICAL);
open PR (NOT_CANONICAL); merged PR with ancestry (CANONICAL); commit absent
(INDETERMINATE); no provenance (INDETERMINATE); content/fingerprint mismatch
(INDETERMINATE); canonical tip unresolved (INDETERMINATE); redaction; the
read-only subprocess invariant; **PR closed without merge (NOT_CANONICAL)**;
**merged PR whose landing SHA is no longer an ancestor (NOT_CANONICAL,
`landing_not_in_canonical_history`)**; **publication branch deleted after a
successful merge (CANONICAL — branch deletion does not erase ancestry-based
canonicalization or the publication record)**; **rebase content-equivalence
(CANONICAL, `merged_pr_content_equivalent` — the candidate is not an ancestor and
no tree-identical commit exists because the base moved, but the changed-path blob
matches the landing commit)**; **content mismatch at landing (NOT_CANONICAL,
`landing_content_mismatch` — the landing SHA is an ancestor of the tip but the
candidate's changed-path content does not match the landing commit's state)**;
**merged PR whose landing SHA is not present locally (INDETERMINATE — fetch is
forbidden, so ancestry cannot be verified)**; **canonical tip known remotely but
its object absent locally (INDETERMINATE, `remote_tip_not_local` — origin
advanced past the last sync)**; **gh unavailable (INDETERMINATE — the PR
merge/landing state is unknown, so "not merged" is not asserted)**; **merged PR
with no landing SHA (INDETERMINATE — gh reported `merged=true` with no
`mergeCommit`)**; and **content-equivalence check unreadable at landing
(INDETERMINATE — a transient `ls-tree` failure on an already-present landing
commit)**. The full suite is 262 tests, all green.

## Status

Implemented and verified locally. An adversarial multi-agent code review
(correctness / security / coverage, each finding independently verified)
confirmed the read-only/security invariant and surfaced four correctness
defects, all the same shape: `_decide`'s fall-through to `NOT_CANONICAL /
not_merged` was too aggressive and swallowed several cases the spec requires to
be `INDETERMINATE`. All four were fixed and each fix pinned by a new hermetic
fixture:

1. **Live ls-remote tip not guaranteed locally present** (high) — origin
   advanced past the canonical checkout's last sync; `merge-base`/`log` against
   the unreadable tip failed silently into a false `not_in_history`. Fixed by a
   `_commit_present` guard on the tip and a distinct `remote_tip_not_local`
   status mapped to `INDETERMINATE` (fixture 18).
2. **gh outage ignored** (medium) — a `gh pr list`/`pr view` failure left an
   `error`/`merge_read_error` field that `_decide` never inspected, so an
   unknown PR state became "not merged". Fixed by an explicit PR-layer-unavailable
   guard → `INDETERMINATE` (fixture 19).
3. **Merged PR with unknown landing SHA** (medium) — `merged=true` with no
   `mergeCommit` left `landing_sha` `None` and fell through to `not_merged` for a
   merged PR. Fixed by a `pr_merged and landing_sha is None and matched is None`
   guard → `INDETERMINATE` (fixture 20).
4. **content_equivalence `unavailable` fell through** (low) — a transient
   `ls-tree` failure on an already-present landing commit became `not_merged`
   instead of inconclusive. Fixed by a `ce_status == "unavailable"` guard →
   `INDETERMINATE` (fixture 21).

The same review also confirmed two required-scenario coverage gaps
(`landing_content_mismatch`, and the merged-PR-landing-absent-locally
`INDETERMINATE` path) that had no test; both are now pinned (fixtures 16 and 17).
A security finding (a `cat-file` call in `verification.py` bypasses the test's
`run_git`/`_git_bytes` spy) was verified as a test-spy scope observation, not a
live defect — the subprocess is the existing read-only `cat-file` on the resolve
path and mutates nothing.

**NOT COMMITTED, NOT PUSHED, NO PR OPENED, LIVE BRIDGELAB NOT RESTARTED.**
Commit, push, live installation/restart, and any real-repository smoke test
require separate Human Steward authorization after review.