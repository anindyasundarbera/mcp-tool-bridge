# M-CC-04 — Merge-vs-ratification (Option A)

## Decision

The bridge must record when an orchestrator or supervisor approves a completed
candidate, but that approval is a **governance act, not a repository act**.
Option A keeps merge (and every other ref-producing action) out of reach: the
`merge` tool is NOT authorized, merge/tag/release/deploy stay in
`PROHIBITED_ACTIONS` (surfaced in every terminal report), and the new surface is
a ratification ledger that records approval without moving any ref.

M-CC-04 adds an **internal-first** `ratify_candidate(project, job_id, actor,
authority_source, detail?)` method plus an additive `ratifications` sidecar
table. **No new MCP tool** — the tool-count canary stays 32.

## Mechanics

- `ratifications` sidecar table (`CREATE TABLE IF NOT EXISTS`, SCHEMA_VERSION
  stays 1): `ratification_id TEXT PRIMARY KEY, project_id, job_id,
  candidate_fingerprint, base_head, actor, authority_source, ratified_at,
  detail_json`.
- `ratify_candidate` validates the job is completed and inactive, derives
  `candidate_fingerprint` from the latest checkpoint's `partial_fingerprint`
  (fail closed when absent), pins `base_head` to the job's retained worktree
  base, and inserts one row. It also stamps the job's audit ledger with a
  `ratified` event carrying an authority block (M-EV-05) recording actor /
  authority source / scope `ratification` / classification `canonical`.
- The terminal snapshot surfaces `ratifications` (bounded list of
  ratification_id, candidate_fingerprint, base_head, actor, authority_source,
  ratified_at) additively, only when a record exists.
- Ratification is durable: a fresh manager re-hydrates the same records from the
  `ratifications` table.
- Distinctness from merge is explicit: `PROHIBITED_ACTIONS` still contains
  `merge` (and `tag`, `force-push`, `push-to-trunk`, `branch-deletion`,
  `ref-move`, `release`, `deploy`), no merge/tag/release/deploy capability is
  added anywhere, and `ratify_candidate` performs no git ref operation at all.

## Scope boundary

- Ratification never creates a commit, never moves HEAD, never pushes, and
  never touches canonical history.
- Internal first: the MCP tool surface is unchanged.
- M-CC-03 (successor jobs) and M-CC-04 compose: a ratified completed candidate
  is the immutable base a successor branches off.
