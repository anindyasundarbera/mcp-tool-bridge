# Getting started

End-to-end: install the bridge, register a project, run it, run a job, verify,
commit, and publish. For the mental model behind these steps, see
[ARCHITECTURE.md](ARCHITECTURE.md). For the authorization schema, see
[CAPABILITIES.md](CAPABILITIES.md). For the security rules, see
[SECURITY.md](SECURITY.md).

---

## Prerequisites

- Python ≥ 3.11
- `git` on PATH (and a git repo to register as a project)
- An ngrok account + authtoken (for the public connector)
- The OpenAI connector mTLS CA bundle (`openai-connectors-mtls-ca.pem`) — this
  is what terminates mTLS at the ngrok edge so only the paired ChatGPT
  connector can reach the bridge.

## 1. Install

```bash
git clone git@github.com:anindyasundarbera/mcp-tool-bridge.git
cd mcp-tool-bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

`pip install -e .` installs `fastmcp>=3.4,<4`, `ngrok>=1.4`,
`pydantic>=2.10`, and `python-dotenv`.

## 2. Configure secrets

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env`:
- `NGROK_AUTHTOKEN` — your ngrok authtoken (the only place it should ever live).
- `NGROK_DOMAIN` — optional reserved ngrok domain for a stable connector URL
  (e.g. `your-name.ngrok-free.dev`). Leave blank for an ephemeral URL that
  changes every run.
- `OPENAI_MTLS_CA_PATH` — path to the mTLS CA bundle (default
  `openai-connectors-mtls-ca.pem`).
- `CLAUDE_MODEL` — the underlying model Claude Code jobs use (default
  `glm-5.2:cloud`). A project can override this per-project with `claude_model`.
- `CLAUDE_LAUNCH_COMMAND` — how the bridge starts the Claude Code drafter
  (shell-split with `shlex`; the standalone `{model}` token is the substitution
  point). Default runs `claude` under `ollama launch`; to use the `claude` CLI
  directly set `CLAUDE_LAUNCH_COMMAND=claude --model {model} -p`. The bridge
  always appends the security tail regardless of this value.
- `CLAUDE_REVIEW_LAUNCH_COMMAND` — same, for the one-shot read-only reviewer
  (defaults to the same prefix).
- `CLAUDE_DEFAULT_EFFORT` — global default effort when a tool call and the
  project both omit one (`high` or `ultracode`; default `high`).

`.env`, `projects.json`, `openai-connectors-mtls-ca.pem`, `.venv`, and
certificates are gitignored and must never enter Git.

## 3. Register a project

```bash
cp projects.example.json projects.json
chmod 600 projects.json
```

A project is an allowlisted git repository. Each entry needs at least `id`,
`display_name`, `description`, `root` (absolute, must be the git top level),
and `capabilities`. Example with verification + commit + push:

```json
{
  "id": "my-app",
  "display_name": "My App",
  "description": "An allowlisted git repository",
  "root": "/home/you/code/my-app",
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
    {
      "id": "pytest",
      "argv": ["{python}", "-m", "pytest", "-q"],
      "python_path": ["src"],
      "kind": "unit"
    }
  ],
  "default_effort": "high",
  "allow_ultracode": true,
  "self_managed": false
}
```

Rules worth knowing now:
- `JOB_WORKTREE_ROOT` (default `~/.local/state/local-dev-mcp/worktrees`) must
  be **outside** every configured project `root`.
- Only the exact `id`s and `capabilities` in this file are usable through MCP.
- Capabilities have dependencies — an invalid file fails at startup, not at
  first use (see [CAPABILITIES.md](CAPABILITIES.md) for the full rules).
- `{python}` in a command argv expands to the interpreter running the bridge;
  a shell executable is rejected.
- A `VerificationCommand` may declare a test `kind` (`unit` default |
  `integration` | `property` | `mutation` | `ad_hoc`). `property` commands run
  under a deterministic `PROPERTY_TEST_SEED` (derived from the base HEAD +
  command id) so property runs are reproducible; `mutation` output parses
  best-effort into advisory `mutation_stats`. An optional `static_analysis`
  block runs declared analyzers + stdlib schema checks read-only at verify time
  (advisory by default; `fail_on: "error"` opts in to failing verification).

Validate the file before starting:
```bash
.venv/bin/python -c "from pathlib import Path; \
  from local_dev_mcp.registry import ProjectRegistry; \
  ProjectRegistry.load(Path('projects.json')); print('registry OK')"
```

## 4. Run

```bash
./run.sh
```

`run.sh` validates `.env`, `projects.json`, and the CA, then launches the
FastMCP server (default `127.0.0.1:8000`) and the ngrok tunnel with prefixed
logs:

```
[server] ...    local_dev_mcp.server.run()
[tunnel] ...    public ngrok endpoint
```

The connector URL printed by the tunnel is `https://<ngrok-domain>/mcp`.
Ctrl+C tears both down cleanly. If `MCP_PORT` is already in use, `run.sh`
detects the server failed to bind and aborts before starting the tunnel.

To run the halves separately:
```bash
python server.py   # FastMCP on 127.0.0.1:8000
python tunnel.py   # ngrok mTLS endpoint
```

## 5. Smoke test

With the server running, the live smoke client lists tools, checks a project,
starts an isolated no-edit job, and polls its result:

```bash
.venv/bin/python test_client.py --project my-app
```

Core unit tests have no network, model, ngrok, launcher, or real-repo dependency:
```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

> Note: a few tool-count canary tests assert the exact tool surface and must be
> bumped when a tool is added or removed. If you add a tool, update the
> expected count in `tests/test_server.py`, `tests/test_checkpoints.py`, and
> `tests/test_persistence.py`.

## 6. A full governed job

From the remote assistant (or any MCP client pointed at the connector URL):

1. `list_allowed_projects` → confirm `my-app` is listed.
2. `get_project_status` → capture the current `HEAD` and confirm the checkout
   is clean.
3. `send_to_local_dev({ project_id, text, effort })` → get a `job_id`. The
   bridge creates a detached worktree at the clean baseline `HEAD` and spawns
   Claude there.
4. `get_development_job` until terminal (`completed`/`failed`/...) or
   `paused_*`. The snapshot includes `prohibited_actions`, the `budget`,
   checkpoints, and the latest candidate fingerprint / changed paths.
5. (optional, `candidate_review`) `list_candidate_changes` →
   `read_candidate_file` to inspect, then `record_candidate_review` (or
   `run_independent_review` if `allow_bridge_reviewer`).
6. `verify_development_job({ project_id, job_id, expected_head })` → a
   single-use `verification_id`, the ordered `paths`, and the fingerprint.
7. `commit_verified_development_job({ ..., verification_id, expected_head,
   expected_diff_fingerprint, paths, message, review_id? })` → a detached
   commit SHA. (Separate human approval; for `requires_review` projects, a
   consumed `review_id` is also required.)
8. (opt-in `push_development_commit`) `push_detached_development_commit({ ...,
   commit_sha, branch_name, expected_base_head, expected_diff_fingerprint })`
   → a new non-trunk remote branch on `origin`.
9. (opt-in `open_pull_request`) `open_pull_request({ ..., base_head,
   fingerprint, title, draft })` → a draft PR for the published branch.
10. (opt-in `sync_canonical_checkout`) after the PR merges on the remote:
    `prepare_canonical_for_work({ project_id, pull: false })` → if
    `updates_available`, `prepare_canonical_for_work({ pull: true })` to
    fast-forward the canonical checkout to `origin/<default>`.

Each step is its own MCP call with its own exact-match confirmation of the
prior state. No single tool silently chains develop → commit → push.

## 7. Durable state & restarts

Job, verification, review, publication, sync, checkpoint, and audit records
persist to `JOB_WORKTREE_ROOT/.state/bridge.db` (SQLite, WAL,
`synchronous=FULL`, file `0o600`, dir `0o700`). A restart re-hydrates
everything synchronously before the event loop starts:

- in-flight job, no checkpoints → reloaded `failed`;
- in-flight job, with checkpoints → reloaded `paused_interrupted` (resumable);
- terminal job, worktree gone → reloaded `failed`;
- completed job with surviving worktree + captured `session_id` → stays
  resumable; single-use verification/review IDs stay valid.

Run only **one** bridge process per `JOB_WORKTREE_ROOT`. The bridge does not
auto-prune orphaned worktrees; manual cleanup is
`git -C <project> worktree remove --force <path>`.

---

## Limits

Every tunable is in `.env` (defaults in `.env.example`), validated as a
positive int/float/port by `Settings.from_env()`.

| Setting | Default | Purpose |
|---|---:|---|
| `MCP_PORT` | 8000 | FastMCP HTTP listen port |
| `CANDIDATE_MCP_PORT` | 8001 | Self-managed candidate probe port (must differ) |
| `MCP_HOST` | `127.0.0.1` | FastMCP bind host |
| `JOB_WORKTREE_ROOT` | `~/.local/state/local-dev-mcp/worktrees` | private root for worktrees + `.state` |
| `CLAUDE_MODEL` | `glm-5.2:cloud` | underlying model for Claude jobs |
| `CLAUDE_LAUNCH_COMMAND` | `ollama launch claude --model {model} --yes -- -p` | drafter launch prefix (`{model}` substituted; security tail always appended) |
| `CLAUDE_REVIEW_LAUNCH_COMMAND` | `ollama launch claude --model {model} --yes -- -p` | reviewer launch prefix |
| `CLAUDE_DEFAULT_EFFORT` | `high` | global default effort (call > project `default_effort` > this) |
| `CLAUDE_TIMEOUT_SECONDS` | 1800 | maximum Claude run time |
| `CLAUDE_MAX_TURNS` | 30 | default per-slice turn limit |
| `CLAUDE_SOFT_TURNS` | 30 | soft warn at this cumulative turn count |
| `CLAUDE_EXTENSION_TURNS` | 30 | turns granted per in-place extension slice |
| `CLAUDE_MAX_EXTENSIONS` | 3 | max in-place extensions before pausing |
| `CLAUDE_MAX_MANUAL_EXTENSIONS` | 5 | budget-extension slices `extend_budget` may grant per job |
| `CLAUDE_HARD_TURNS` | 120 | absolute cumulative turn cap |
| `CLAUDE_MAX_NO_PROGRESS_TURNS` | 6 | pause when changed-path set is unchanged for this many turns |
| `CLAUDE_MAX_TOOL_FAILURES` | 5 | terminate as `failed_infrastructure` above this many tool failures |
| `CLAUDE_TOOL_FAILURE_RETRIES` | 2 | retries per failing slice (fresh executor) before the cumulative tool-failure ceiling classifies the job; 0 disables retry |
| `CLAUDE_HARD_WALL_SECONDS` | 0 | cumulative wall-time cap across all slices; 0 disables |
| `CLAUDE_CHECKPOINT_INTERVAL_SECONDS` | 30 | progress-sampler cadence (may be fractional) |
| `CLAUDE_CHECKPOINT_MAX` | 500 | checkpoint count that triggers compaction |
| `CLAUDE_OSCILLATION_WINDOW` | 4 | consecutive checkpoints compared for the oscillating-edits trigger |
| `CLAUDE_OSCILLATION_ALTERNATIONS` | 2 | content alternations within the window that flag oscillation |
| `CLAUDE_SCOPE_GROWTH_FACTOR` | 0.0 | changed-path growth factor before progress is discounted; 0.0 disables |
| `CLAUDE_PROGRESS_INTEL_WINDOW` | 8 | cap on recent checkpoints loaded for progress intelligence |
| `CHECKPOINT_RETENTION_DAYS` | 0 | time-based GC of non-promoted checkpoint rows; 0 disables |
| `CLAUDE_STREAM_JSON` | off | drive Claude with `--output-format stream-json --verbose` for real counts |
| `MAX_INPUT_CHARS` | 100000 | maximum job instruction length |
| `MAX_OUTPUT_BYTES` | 2097152 | maximum Claude stdout |
| `MAX_MARKDOWN_FILE_BYTES` | 1048576 | maximum reader file size |
| `MAX_MARKDOWN_LIST_RESULTS` | 500 | maximum Markdown listing results |
| `MAX_MARKDOWN_SEARCH_RESULTS` | 100 | maximum search results |
| `MAX_MARKDOWN_RANGE_LINES` | 1000 | maximum lines per range read |
| `MAX_MARKDOWN_WRITE_BYTES` | 1048576 | maximum Markdown write/current size |
| `MAX_MARKDOWN_DIFF_BYTES` | 262144 | maximum returned writer diff |
| `READER_INJECTION_DEFENSE` | off | fence + delimit untrusted repo content the reader ingests |
| `GIT_COMMAND_TIMEOUT_SECONDS` | 30 | maximum fixed git command time |
| `CANDIDATE_HEALTH_TIMEOUT_SECONDS` | 30 | candidate startup/probe deadline |
| `VERIFICATION_COMMAND_TIMEOUT_SECONDS` | 600 | per-command verification deadline |
| `MAX_VERIFICATION_OUTPUT_BYTES` | 1048576 | captured stdout/stderr per stream |
| `RUNTIME_VERIFICATION_COMMAND_TIMEOUT_SECONDS` | 1200 | runtime-check deadline |
| `MAX_RUNTIME_VERIFICATION_OUTPUT_BYTES` | 1048576 | runtime-check captured output |
| `MAX_WORKING_TREE_CAPTURE_MANIFEST_PATHS` | 500 | max manifest paths in a capture |
| `MAX_WORKING_TREE_CAPTURE_FILE_BYTES` | 10485760 | max single file in a capture |
| `VERIFICATION_DETERMINISTIC_REPETITIONS` | 1 | deterministic repeat count for verification |
| `MAX_DELEGATIONS_PER_JOB` | 3 | bounded sub-agent delegations per job |
| `MAX_COMMIT_MESSAGE_CHARS` | 500 | maximum approved commit message length |
| `EVIDENCE_RETENTION_DAYS` | 0 | policy ceiling for evidence rows (verifications/reviews/publications/syncs); GC never deletes promoted evidence; 0 disables |
| `ENABLE_DURABLE_AUTHZ` | off | persist authorization objects to the `authorizations` table |
| `REQUIRE_APEX_GROUNDING` | off | when set, reject any enabled project whose `apex_authority_ref` is missing (M-GV-03) |