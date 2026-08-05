# Next session handoff — review MCPB-004

Updated: 2026-08-05

## Authoritative project

- GitHub: `https://github.com/anindyasundarbera/mcp-tool-bridge`
- Branch: `feature/updatingCapabilities`
- Pushed MCPB-003 baseline inspected for this implementation:
  `9cee986b4b62db6fc26e974642754793f7f71776`
  (`MCPB-003 — Hash-protected Markdown Writer`)
- Every one of the 34 baseline source blobs matched the pushed branch before
  implementation.
- ChatGPT project/application: `mcp-chatgpt-plugin`
- The prepared MCPB-004 source is local only. It has not been committed,
  pushed, installed on the live bridge, or invoked through a modifying live
  connector tool.

## Prepared MCPB-004 implementation

- Changed `send_to_local_dev` into an asynchronous start operation returning a
  bridge-generated job ID.
- Added `get_development_job`, `resume_development_job`, and
  `cancel_development_job` with exact project/job authorization and schemas.
- Added clean-baseline-gated detached worktrees beneath a private configured
  root, with source/worktree HEAD checks and fixed bounded Git commands.
- Claude edits only the job worktree. All jobs and Markdown writes share the
  per-project modifying lock.
- Added in-memory lifecycle state, bounded/redacted terminal evidence,
  process-group cancellation, and retained worktrees.
- Resume accepts no raw session ID; it can use only the valid session captured
  from the same terminal bridge job and retained worktree.
- Self-managed projects may enable isolated `claude_edit` but still cannot
  enable direct `markdown_write`.
- Successful self-managed runs start a short-lived candidate server on a
  separate localhost port, initialize it through MCP, verify the expected tool
  surface, stop it, and retain the candidate. Candidate startup uses a minimal
  environment allowlist rather than inheriting production secrets.
- Added four worktree/candidate settings and updated package version to `0.4.0`.

## Changed files

Modified:

- `.env.example`
- `AGENTS.md`
- `README.md`
- `projects.example.json`
- `pyproject.toml`
- `src/local_dev_mcp/__init__.py`
- `src/local_dev_mcp/claude.py`
- `src/local_dev_mcp/config.py`
- `src/local_dev_mcp/git.py`
- `src/local_dev_mcp/models.py`
- `src/local_dev_mcp/registry.py`
- `src/local_dev_mcp/server.py`
- `test_client.py`
- `tests/test_claude.py`
- `tests/test_config.py`
- `tests/test_registry.py`
- `tests/test_server.py`

Added:

- `docs/MCPB-004-IMPLEMENTATION-BRIEF.md`
- `docs/MCPB-004.md`
- `docs/NEXT-SESSION-MCPB-004.md`
- `src/local_dev_mcp/candidate.py`
- `src/local_dev_mcp/jobs.py`
- `src/local_dev_mcp/worktrees.py`
- `tests/test_candidate.py`
- `tests/test_jobs.py`
- `tests/test_worktrees.py`

## Verification completed

- The pushed MCPB-003 baseline passed all 36 existing tests before edits.
- The final MCPB-004 core suite passed all 51 tests in 0.899 seconds with
  temporary Git repositories and fake Claude/candidate processes.
- Production entry points compiled; the fresh wheel imported the server, job,
  worktree, and candidate modules successfully.
- FastMCP exposed 13 tools. All three new lifecycle tools have exact
  32-character lowercase hexadecimal job-ID patterns; resume has no public
  session-ID parameter; annotations and effort defaults/enums match the brief.
- The sdist and wheel built successfully. A fresh wheel environment imported
  version `0.4.0`, and `pip check` reported no broken requirements.
- Source, sdist, and wheel audits found no real `.env`, credentials,
  certificates, `projects.json`, virtual environments, caches, worktrees, or
  stale build output. Package hashes from the final build were:
  - sdist: `443d3492a70507a9a2c1ce23e87a197daca2d9c554990c9172a06816da1fb1bd`
  - wheel: `dac0c6bdef4224de42cb6e84780c05cc911c1a5182a243fd3f4efc7156f43e67`
- The live connector was read only for this session: its MCPB-003 tool surface
  and allowed project list were confirmed, but no modifying tool was invoked.

Live Ollama/Claude, candidate-server, ngrok, and real-repository smoke checks
remain separate and must not be inferred from core tests.

## Review boundary

MCPB-004 does not add deterministic project verification, diff fingerprints,
staging, commits, branches, push, merge, tags, candidate promotion, production
overwrite/restart, rollback switching, worktree deletion through MCP,
unrestricted Bash, arbitrary shell commands, or caller-supplied session IDs.

Job IDs and resume authority are in memory for this phase. A server restart
invalidates them while leaving retained worktrees on disk for manual recovery
or later MCPB-005 handling.

## Next actions

1. Review the prepared source, lifecycle contracts, and test coverage.
2. Run any desired live no-edit job and self-managed candidate checks on the
   user's machine as separate tests.
3. If accepted, explicitly authorize any desired commit/push and live
   installation/restart as separate actions.
4. After MCPB-004 acceptance, write the MCPB-005 implementation brief before
   adding verification or commit authority.
