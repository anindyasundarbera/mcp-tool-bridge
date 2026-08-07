# MCPB-008 — Bounded detached-commit publication

## Decision

Before MCPB-008, BridgeLab could verify a candidate and create a detached
commit (`commit_verified_development_job`), but the commit lived only in the
retained job worktree's object database. No branch or remote ref was ever
created, so a reviewed and verified candidate could not reach GitHub. The
closure chain ended at `verify → commit`; there was no `publish` step.

MCPB-008 adds one bounded, opt-in publication capability, **dormant by
default** — a project that does not opt in observes no new tool and no
broadened permission:

`push_detached_development_commit(project_id, job_id, commit_sha, branch_name,
expected_base_head, expected_diff_fingerprint)` pushes **exactly** the
bridge-created detached `commit_sha` to a **new** `refs/heads/<branch_name>`
on the `origin` remote. It:

- pushes exactly `commit_sha` via the refspec `<commit_sha>:refs/heads/<branch>`,
  with no `--force`, `--tags`, `--all`, or `--mirror`;
- rejects the repository default branch, `main`, and `master`;
- requires the remote branch not to exist;
- never force-pushes, merges, tags, deletes, or moves another ref;
- confirms the commit belongs to the specified retained job
  (`job.committed_sha == commit_sha`) and realizes the verified candidate
  fingerprint (re-derived directly from the commit object); and
- requires a **used** verification record for the exact
  `(base_head, diff_fingerprint)` to exist — i.e. the candidate passed
  `verify → commit`.

It returns the remote branch name, the commit SHA, and the repository URL.
Publication is **one branch per job**: a retry with the same
`(commit_sha, branch_name)` is idempotent (and re-pushes if the remote ref
vanished); publishing the same job to a second, different branch is rejected.

## Reconciliation

Publication is the outer link of the existing chain. It reuses the per-project
lock, the durable `bridge.db`, exact-evidence binding, path redaction, and git
run through `run_git` / `_git_bytes`. It adds **one** capability
(`push_development_commit`, which requires `verification_commit`), **one**
tool, **one** additive `publications` table, and **one** new manager
(`PublicationManager`). `SCHEMA_VERSION` stays 1: the `publications` table is
created with `CREATE TABLE IF NOT EXISTS`, so existing databases upgrade in
place (per the documented MCPB-006/007 additive philosophy). `PublicationManager`
re-hydrates publications synchronously in `__init__` and persists each
publication as one ACID transaction, exactly as the job, verification, and
review managers do. No existing tool, parameter, or safety annotation changes
for projects that do not opt in.

A new `committed_state(...)` helper re-derives the candidate fingerprint
directly from the `commit_sha` git object (tree vs. parent) over the verified
paths, mirroring `_staged_state` / `_candidate_state` and reusing their private
helpers (`_FINGERPRINT_MARKER`, `_frame`, `_digest_git_blob`,
`_validate_policy_path`, `_git_bytes`). Because `commit_verified_development_job`
commits a tree equal to the staged state it verified, the fingerprint computed
here equals the one bound to the verification record, so publication proves
the committed object realizes the verified candidate without trusting the jobs
table's `committed_sha` alone.

## Security

Every guard is enforced in server code, never in model instructions:

- **Exactly the verified commit.** The refspec is
  `<commit_sha>:refs/heads/<branch_name>` with no force flag; `commit_sha` is
  validated as 40-hex and resolved as a real commit; the remote ref is
  post-verified with `ls-remote` to equal `commit_sha`. Hooks are disabled with
  `-c core.hooksPath=/dev/null` (mirrors `commit()`).
- **Belongs to the retained job.** `committed_candidate` requires a completed,
  inactive job with a bridge-set `committed_sha` and a retained worktree;
  `commit_sha` must equal it.
- **Matches the verified fingerprint.** `committed_state` recomputes the
  fingerprint from the commit object and compares it to
  `expected_diff_fingerprint`; the commit's parent must equal
  `expected_base_head`; its `diff-tree` path set must equal the record's paths.
  Separately, `used_verification_record` confirms a **used** verification
  record for the exact evidence exists. The re-derivation is the content proof;
  the record lookup is the authorization proof; both must pass.
- **Branch protections.** `_validate_branch_name` rejects full refs, `HEAD`,
  leading/trailing or consecutive slashes, leading dots, `..`, `.lock`
  suffixes, `@{`/`@`, control characters, and git-refname-forbidden characters;
  `main`/`master` and the repo's default branch (best-effort via
  `refs/remotes/origin/HEAD`) are rejected.
- **Remote branch must not exist.** Pre-checked with `ls-remote --heads`; the
  non-force push would also reject a non-fast-forward, but the pre-check gives a
  clear error.
- **One branch per job.** The `publications` row keyed by
  `(project_id, job_id)` makes publish single-use-per-branch: a same-branch
  retry re-verifies via `ls-remote` and returns idempotent success (re-pushing
  only if the remote ref vanished); any other branch/commit is rejected.
- **Output hygiene.** All git output is redacted with `_redact` over the full
  path set; only the short branch name, `commit_sha`, and remote URL are
  returned.

## Trust boundary (documented)

Publication is the operator's explicit, outer-most authorization: it forwards
an already-reviewed, already-verified, already-committed object. The bridge
does **not** create the commit in this step (`commit_verified_development_job`
still owns that), does not open a pull request, does not merge, and does not
update a trunk. The single-branch-per-job bound means one verified candidate
becomes exactly one published ref; an operator who wants the same change on a
second branch must run a second verified job. The `origin`-only remote
constraint (fail closed if `origin` is absent) keeps publication to the
project's configured canonical remote.

## Explicitly excluded

- Force-push, merge, tag, PR creation, trunk update, deletion, worktree
  cleanup, or a configurable remote (`origin` only).
- Publishing to an existing remote branch, the default branch, `main`, or
  `master`.
- Publishing a commit that was not created by `commit_verified_development_job`
  for the specified retained job, or whose tree does not realize the verified
  fingerprint.
- Any new surface for projects that do not opt in (`push_development_commit`
  is off by default and requires `verification_commit`).
- A schema-version bump; `publications` is additive (`SCHEMA_VERSION` stays 1).