# M-GV-06 — Capability-subset runtime enforcement (delegated children)

## Decision

A delegation mandate bounds a child two ways: its `outputs` limit which paths
the child may change (enforced at completion in `_run`), and its `authority`
list declares which capabilities the child participates in. Before this
milestone the `authority` list was recorded but never enforced at runtime — an
operator who obtained a child's `job_id` could drive that child through any
capability the *project* held (verify, commit, publish, open a PR, export an
evidence bundle), regardless of the mandate.

M-GV-06 makes the subset real at the runtime boundary. The change is
**additive** — no schema change, no new tools, no new push surface:

- `DevelopmentJobManager.require_job_capability(project, job_id, capability)`
  is the enforcement primitive. A plain (non-delegated) job has no mandate and
  is governed only by the caller's existing project-level capability gate; a
  delegated child is additionally bounded to the capability names its mandate
  `authority` list declares — an invocation outside that subset is rejected
  with a clear error. **Fail-closed**: a child whose mandate has no `authority`
  list is authorized for nothing at the job scope.
- Every job-scoped MCP handler in `server.py` calls it right after its
  `ProjectRegistry.resolve` gate, so the project must still hold the capability
  before the child's subset is consulted: `get_development_job` /
  `resume_development_job` / `cancel_development_job` (claude_edit),
  `verify_development_job` / `commit_verified_development_job`
  (verification_commit), `list_candidate_changes` / `read_candidate_file` /
  `record_candidate_review` / `run_independent_review` / `get_candidate_review`
  (candidate_review), `push_detached_development_commit`
  (push_development_commit), `export_evidence_bundle`
  (export_evidence_bundle), `open_pull_request` (open_pull_request).

The delegation itself (M-GV-06 core) still requires `parent_job_id` + a
non-empty `outputs` mandate; a child with a NULL mandate is rejected at
completion rather than promoted unbounded.

## Security boundary

The subset check is fail-closed: no authority list means no job-scoped
capability. It composes with (never replaces) the project-level capability gate
and the existing durable per-action authorization for commit/push. A child's
authority list is bounded (≤100 entries, non-empty strings) at delegation time
by `_validate_mandate`.
