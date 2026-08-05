# Next session handoff — review MCPB-005

Updated: 2026-08-05

## Authoritative project

- GitHub: `https://github.com/anindyasundarbera/mcp-tool-bridge`
- Branch: `feature/updatingCapabilities`
- Accepted and pushed MCPB-004 baseline:
  `ca63a64f207c8e59207995b467244a98a5b92d4d`
  (`MCPB-004 — Isolated asynchronous development`)
- The baseline source archive used here has SHA-256
  `1779e767a5534ffd4923902204ac48605b7de011c6b551bf3aa98dbb7130fbe7`.
- ChatGPT project/application: `mcp-chatgpt-plugin`
- The prepared MCPB-005 source is local only. It has not been committed,
  pushed, installed on the live bridge, restarted, or invoked through a
  modifying live connector tool.

## Prepared MCPB-005 implementation

- Added the `verification_commit` project capability with required
  `commit_roots` and registry-fixed `verification_commands`.
- Added `verify_development_job`, which accepts no command input, requires a
  completed retained job at its exact base HEAD, rejects pre-staged and unsafe
  paths, runs direct argv vectors in a minimal environment, and returns bounded
  structured command evidence.
- Added staging-invariant SHA-256 fingerprints covering every changed path's
  HEAD entry and current file mode/content or deletion marker.
- Successful verification requires every command to pass without changing the
  candidate and returns a single-use bridge verification ID.
- Added `commit_verified_development_job`, which requires a separate human
  confirmation plus exact verification ID, HEAD, fingerprint, ordered paths,
  and commit message.
- Commit rechecks the candidate, stages only `git add -- <paths>`, reconstructs
  the fingerprint from index blobs, disables hooks and signing, and proves the
  resulting clean commit remains detached with the verified base as its parent.
- Failed staging/commit restores an empty index while preserving candidate
  files. A committed job cannot resume, and verification IDs cannot replay.
- Candidate health now requires the two MCPB-005 tools.
- Package version is `0.5.0`.

## Changed files from MCPB-004

Modified:

- `.env.example`
- `AGENTS.md`
- `README.md`
- `projects.example.json`
- `pyproject.toml`
- `src/local_dev_mcp/__init__.py`
- `src/local_dev_mcp/candidate.py`
- `src/local_dev_mcp/config.py`
- `src/local_dev_mcp/jobs.py`
- `src/local_dev_mcp/models.py`
- `src/local_dev_mcp/server.py`
- `test_client.py`
- `tests/test_config.py`
- `tests/test_registry.py`
- `tests/test_server.py`

Added:

- `docs/MCPB-005-IMPLEMENTATION-BRIEF.md`
- `docs/MCPB-005.md`
- `docs/NEXT-SESSION-MCPB-005.md`
- `src/local_dev_mcp/verification.py`
- `tests/test_verification.py`

## Verification completed

- The pushed MCPB-004 baseline passed all 51 existing tests before edits.
- The complete MCPB-005 suite passes all 61 tests with temporary repositories,
  fake Claude/candidate processes, and fixed local verification commands.
- Coverage includes tracked edits/additions/deletions, binary and executable
  files, hidden/protected/outside-root paths, symlinks, pre-staged state,
  command failure/state drift/timeout/output bounds, evidence mismatch and
  replay, injected commit failure/index restoration, Git hook suppression,
  exact commit parent/path checks, detached HEAD, source-ref immutability, and
  shared-lock serialization.
- FastMCP exposes exactly 15 tools. Verification is destructive,
  non-idempotent, and open-world; commit is destructive, non-idempotent, and
  closed-world. Job, verification, HEAD, and fingerprint patterns plus the
  bounded path schema match the implementation brief.
- Production entry points compile and the fresh wheel imports the server and
  verification manager at version `0.5.0`.
- The sdist and wheel build successfully. A fresh wheel installation passes
  `pip check`. Package hashes are:
  - sdist: `a55f4c1c07068d407291676ef3127c9a38633d4ef3d3854e89f7c69f03e74704`
  - wheel: `4832cf9c4b77c4a41b9997465bb4b0f7942ddb09a96e8d94fd5e610ccd97a9d0`
- Source, sdist, and wheel audits found no real `.env`, private project
  registry, credentials, certificates, virtual environments, caches,
  worktrees, or stale build output.

Live Claude/Ollama, candidate-server, ngrok, real-repository verification and
commit, installation, and restart checks remain separate and must not be
inferred from core tests.

## Review boundary

MCPB-005 adds no caller-supplied command, arbitrary environment, unrestricted
Bash, branch/ref update, push, merge, tag, pull request, promotion, production
overwrite/restart, rollback switch, deletion, or worktree cleanup. The commit
tool advances only the retained detached worktree HEAD after verification and
separate human approval.

## Next actions

1. Review the prepared source, tool contracts, and final project ZIP.
2. If accepted, explicitly authorize any desired GitHub commit/push.
3. Separately update the live private `projects.json` to opt the self-managed
   project into `verification_commit`, install/restart the bridge, and run a
   real isolated candidate verification/commit smoke test if desired.
4. Promotion or production switching requires a new explicit design and is not
   implied by MCPB-005 completion.
