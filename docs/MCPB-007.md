# MCPB-007 — Independent candidate review

## Decision

Before MCPB-007, BridgeLab could verify a candidate (run the project's
verification commands and bind a `(base_head, diff_fingerprint, paths)` triple)
but it could not expose that retained candidate to a *separate* reviewer: the
only thing a reviewer could see was the project's canonical checkout, and the
semantic self-review was performed by the drafting agent itself. There was no
bridge surface to read the candidate, no record of a review, and no way to make
a review a precondition of a commit.

MCPB-007 adds three opt-in layers, all **dormant by default** — an existing
project that does not opt in observes no new tools, no schema change, and no
broadened permission:

1. **Bounded candidate reads.** Two read-only tools — `list_candidate_changes`
   and `read_candidate_file` — expose the *retained* worktree's changed paths to
   a reviewer, scoped to exactly the candidate's changed-path set (re-derived on
   every call from the same `candidate_changed_paths` helper that
   `_candidate_state` uses, so review and verification never disagree on the
   changed set). Reads use `_safe_path` with `O_NOFOLLOW` and a bounded
   read loop; absolute, `..`, empty, and non-canonical paths are rejected. Gated
   on the new `candidate_review` capability, which itself requires `claude_edit`.

2. **Review records + an opt-in commit gate.** `record_candidate_review` records a
   verdict bound to the candidate's `(base_head, diff_fingerprint, paths)` triple
   in an additive `reviews` table (no schema-version bump); `get_candidate_review`
   retrieves it. `commit_verified_development_job` gains an optional `review_id`.
   When `requires_review` is set (which requires `verification_commit`), a commit
   must present a matching, still-unused review: `validate_for_commit` runs *before*
   the commit (exact triple match, single-use) and `mark_used` flips the record
   *after* the commit succeeds. The review is advisory evidence — the human still
   approves the exact verification evidence and message; the detached-commit-only
   semantics are unchanged and nothing is pushed, merged, or tagged.

3. **A bridge-spawned, read-only reviewer.** `run_independent_review` launches a
   *fresh* Claude session via `build_reviewer_command` — no `--resume`, tools
   `Read,Glob,Grep`, `permission-mode default`, no `acceptEdits` — so the reviewer
   is provably distinct from the drafter, is read-only, cannot mutate the
   candidate, and is never resumable (no `session_id` is returned). It binds
   `_candidate_state` before and after the run and rejects drift. Gated on
   `allow_bridge_reviewer`, which requires `candidate_review`.

## Reconciliation

Review records are persisted to the existing `bridge.db` in a new, additive
`reviews` table (`SCHEMA_VERSION` stays 1). `ReviewManager` re-hydrates records
synchronously in `__init__` before the loop starts, exactly as the job and
verification managers do, and persists each mutation as one ACID transaction.
The `open_state_db` version seam is fixed: a stored version *older* than
`SCHEMA_VERSION` is advanced on open (a no-op today, correct for a future bump);
a newer version still fails closed. No new MCP tool, tool parameter, or safety
annotation changes for projects that do not opt in; the five new tools and the
new `review_id` parameter are inert unless the project enables the capability.

## Security

No exposure widening for existing projects: every new surface is capability- or
policy-gated and off by default. Candidate reads are scoped to the changed-path
set and use an end-to-end-anchored open (`_open_candidate_read`) that walks the
directory chain with `O_DIRECTORY|O_NOFOLLOW` and opens the leaf *relative to the
validated parent descriptor* — so a reviewer cannot traverse the worktree, follow
symlinks (leaf or parent), or reach the canonical checkout. Every output is
redacted with `_redact` over the full path set (worktree path, project root,
`job_worktree_root`, config path, all project roots); the worktree absolute path
and the reviewer's `session_id` are never returned, and a worktree-root open
failure raises a path-free error rather than leaking the absolute path via
`OSError.filename`. Separation of duties holds for the spawned reviewer: it is a
fresh, read-only, non-resumable session, and records the reserved
`reviewer_id="bridge-reviewer"` (which the caller path cannot forge) so an
operator can tell an attested review from a caller-supplied one. Lock ordering is
`project → reviews` for `record`/`run` and `reviews`-only for
`validate_for_commit`/`mark_used`, so there is no reverse ordering and no
deadlock. The commit gate is safe because the single-use `verification_id`
remains the true authority: even if `mark_used` failed to persist after a
successful commit, the review would stay reusable but no second commit is
possible.

## Trust boundary (documented)

`record_candidate_review` is **caller-supplied**: the bridge records the verdict
the caller passes and binds it to the candidate's state, but it does **not**
attest *who* reviewed — a drafter could in principle record its own verdict and
satisfy `requires_review`. This is intentional: `record_candidate_review` is the
route for an *external* (human or out-of-band) reviewer to lodge a verdict
through the bridge. The **strong, attested** guarantee of independence is
`run_independent_review`, which the bridge spawns and can vouch for. An operator
who wants a provably-independent reviewer enables `allow_bridge_reviewer` and
uses that tool; an operator who trusts an external reviewer's channel uses
`record_candidate_review`. The two are complementary, and the difference is
documented rather than hidden.

## Explicitly excluded

- Mandatory review for projects that do not opt in (`requires_review` is `False`
  by default; `candidate_review`/`allow_bridge_reviewer` are off by default).
- Any new dependency, vector store, or persistence backend (the additive
  `reviews` table reuses `bridge.db`).
- A schema-version bump (the seam is fixed; `SCHEMA_VERSION` stays 1).
- Push, merge, tag, branch/ref update, promotion, production overwrite/restart,
  rollback switching, deletion, or worktree cleanup through MCP — unchanged.
- Attesting the identity of a caller that uses `record_candidate_review` (the
  trust boundary above is documented, not enforced by identity).