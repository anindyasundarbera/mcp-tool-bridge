# Next session handoff — BridgeLab capability elevation (tasks 1–8 done)

Updated: 2026-08-11

## Authoritative project

- GitHub: `https://github.com/anindyasundarbera/mcp-tool-bridge`
- ChatGPT project/application: `mcp-chatgpt-plugin`
- Active development branch: `feature/updatingCapabilities`
- This session's work lives on **per-task feature branches** (see table below),
  each pushed to `origin`. The current checkout is on
  `feature/mcpb-cc01-pull-request` (the last task completed).

## What this session accomplished

This session implemented the first **8 of 12** tasks from
`IMPLEMENTATION_PROGRESS.md` (the BridgeLab capability-elevation plan driven by
`docs/MCPB-CAPABILITY-ELEVATION.html`). Each task was implemented on its own
branch, committed, pushed, and security-reviewed. All work is internal-first
except where a task is explicitly flagged **NEW SURFACE** (a new MCP tool +
capability), which the user authorized.

### Completed tasks and their branches

| # | Task | Status | Branch | Commit |
|---|---|---|---|---|
| 1 | M-EV-01 requirement traceability | ✅ | `feature/mcpb-010-continuity` | `4596ceb` |
| 2 | M-CT MCPB-010 tail (fetch+pin base, atomic milestones, dormant states) | ✅ | `feature/mcpb-010-mct-tail` | `8d863c7` |
| 3 | M-CC-09 verification-lab determinism | ✅ | `feature/mcpb-009-verification-lab` | `7186368` |
| 4 | M-CC-11·M-EV-06 evidence bundle | ✅ (bundle; supply-chain deferred) | `feature/mcpb-ev06-evidence-bundle` | `9422774` |
| 5 | M-GV-06 sub-agent delegation | ✅ | `feature/mcpb-gv06-delegation` | `16fd2e7` |
| 6 | M-GV-01/02/03/04 durable auth + roles | ✅ (M-GV-01; 02-04 deferred) | `feature/mcpb-gv01-authorization` | `1163a3a` |
| 7 | M-GV-07 parallel verify + review reconciliation | ✅ (reconcile; fan-out deferred) | `feature/mcpb-gv07-parallel-review` | `1a8bfe4` |
| 8 | M-CC-01/02/05 PR creation + idempotency | ✅ | `feature/mcpb-cc01-pull-request` | `f80c05a` |

`IMPLEMENTATION_PROGRESS.md` is updated to mark tasks 1–8 complete.

### New MCP tools added this session (NEW SURFACE, all dormant by default)

- `export_evidence_bundle` (M-EV-06) — capability `EXPORT_EVIDENCE_BUNDLE`
  (requires `repository_read`).
- `open_pull_request` (M-CC-01) — capability `OPEN_PULL_REQUEST` (requires
  `push_development_commit`).

Tool count went 22 → 24. The guard tests in `tests/test_server.py`,
`tests/test_checkpoints.py`, `tests/test_persistence.py` assert the count and
were bumped accordingly.

## Test status

- Full suite: **209 passed, 1 failed, 35 subtests passed**.
- The single failure (`tests/test_server.py::ServerSchemaTests::
  test_exposes_reader_tools_with_read_only_annotations`) is **pre-existing and
  environmental, NOT a regression**: the entire `/` filesystem is mounted
  read-only (`OSError 30, Read-only file system`), so the test's default state
  DB path (`~/.local/state/local-dev-mcp/worktrees/.state/bridge.db`) cannot be
  opened for writing. Reproduced identically on the base commit `ee696d2`.
  `/tmp` is writable, which is why all temp-dir tests pass.
- New test files added this session: `tests/test_mct.py` (6),
  `tests/test_verification_lab.py` (4), `tests/test_evidence_bundle.py` (4),
  `tests/test_delegation.py` (9), `tests/test_authorizations.py` (10),
  `tests/test_pr.py` (6). Plus new tests appended to `tests/test_review.py`
  (4 reconciliation) and `tests/test_config.py` (defaults).

## Security reviews

Every task went through a security review; the flagged issues were fixed before
commit. Notable fixes:
- M-EV-06: raw verification command stdout/stderr no longer leaks into the
  bundle (only status/exit_code + output SHA-256 hashes); bundle SHA-256 is
  recomputable over the returned bytes.
- M-GV-06: delegation mandate is required + validated, one-level depth bound,
  fail-closed scope enforcement (child changed paths must stay within the
  mandate's declared outputs), legacy NULL-mandate child rows fail closed.
- M-GV-01: single-use authorization consumption is TOCTOU-safe (atomic
  `WHERE used = 0` guard + `check_and_consume`).
- M-GV-07: divergent-review tie-breaker requires a **bridge-attested** reviewer
  (`_BRIDGE_REVIEWER_ID`, minted only by `run_independent_review`), not
  caller-supplied `reviewer_id` distinctness (closes a forgery hole).
- M-CC-01: `gh` CLI used with fixed argv (no shell); merge never issued;
  discover-then-create/update is race-safe; gh failures during discovery are
  re-raised (never mistaken for "no PR"); `headRefName` cross-checked;
  fingerprint re-validated against a used verification record; git failures
  wrapped as `PullRequestError`.

## Environment notes

- The system SSH config is broken (`/etc/ssh/ssh_config.d/` owned by
  `nobody`), so plain `git push` fails. Use:
  `GIT_SSH_COMMAND="ssh -F /dev/null -o StrictHostKeyChecking=no" git push ...`
- The `/` filesystem is read-only; use `/tmp` for scratch/build/venv work.
- The package version is inconsistent (pre-existing): `pyproject.toml` says
  `0.10.0`, but `src/local_dev_mcp/__init__.py.__version__` is `0.8.0`. Not
  changed this session — flag for the user.
- `openai-connectors-mtls-ca.pem` is tracked in Git (committed in the initial
  `0f5ec58`), which violates AGENTS.md ("certs must never enter Git"). Not
  removed (destructive history rewrite) — flag for the user.

## Untracked scratch material (do NOT commit)

`DEVELOPMENT_GUIDE.md`, `MANUAL_INSTALLATION_GUIDE.md`, and the third-party
reference clones `LightRAG/`, `RAG-Anything/`, `tokengate/` (each a nested
upstream git repo with its own `.venv`). These are unrelated to the elevation
work and were deliberately left untracked.

## Next task: #9 — M-CC-11 prompt-injection defense

The next pending task in `IMPLEMENTATION_PROGRESS.md` is:

### 9. M-CC-11: Prompt-injection defense for repo-ingested content
- **Status**: Pending
- **Description**: Systematic injection defense on content the reader ingests
- **Pattern Source**: NeMo-Guardrails · llm-guard (fork)
- **Key Elements**:
  - Borrow spotlighting/data-trust-boundary technique (fence + delimit
    untrusted repo content)
  - NeMo-Guardrails' programmable rails or a maintained llm-guard fork as
    implementation
  - Run as fixed-argv on reader output (not networked service)
  - Internal hardening; no new tool surface
  - Hardens `reader.py`

**Suggested approach** (mirroring the established per-task pattern):
1. Create a new branch off the current tip, e.g.
   `feature/mcpb-cc11-injection-defense`.
2. Implement the spotlighting/fencing in `src/local_dev_mcp/reader.py` (fence +
   delimit untrusted repo content the reader ingests, so model instructions
   can't ride in file contents). Internal, opt-in, no new tool.
3. Add tests in `tests/test_reader.py` (a repo file containing an injection
   attempt is fenced/neutralized; a normal file is unchanged).
4. Run the full suite, security-review, commit, and push.

## Remaining tasks after #9 (from IMPLEMENTATION_PROGRESS.md)

- **10. M-EV-05**: Authority-behind-every-governance-instruction (span-attribution
  model; stamp governance events with `authority_ref`; sidecar
  `authority_records` + `source_classifications`). Internal, no new tool.
- **11. M-CC-11**: Supply-chain gate — severity-gated pre-commit reject
  (syft + grype `--fail-on=high` + pip-audit). Internal, fixed-argv.
- **12. M-CC-11**: Sandboxed execution of repo-controlled scripts
  (bwrap/nsjail/gVisor with `--network=none --read-only`). Internal, no new tool.

## Deferred sub-items (noted in IMPLEMENTATION_PROGRESS.md)

- M-EV-06 supply-chain scan (needs a runtime scanner; separate opt-in).
- M-GV-02/03/04 named roles + external apex grounding + spec-gating.
- M-GV-07 parallel fan-out (verify + review via `asyncio.gather`) and wiring
  reconciliation into the commit gate.
- M-CC-04 merge-vs-ratification (Option B needs explicit user authorization).

## Session-start checklist for the next session

1. Read `AGENTS.md` completely.
2. Read `IMPLEMENTATION_PROGRESS.md` and this handoff.
3. Inspect `git status`, branch, and the per-task branch table above.
4. Run the baseline tests (expect 209 passed / 1 pre-existing read-only
   failure).
5. Implement only the active task (#9 next), on its own branch, with tests,
   security review, commit, and push.
