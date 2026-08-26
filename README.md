# Local Development MCP (BridgeLab)

A **project-aware MCP bridge** that lets a remote assistant (ChatGPT) safely
delegate engineering work to a local Claude Code runtime on allowlisted Git
repositories, with deterministic verification, human-gated commits, and bounded
publication — all enforced in server code, never by model instructions.

ChatGPT addresses projects by a stable `project_id`. The bridge resolves those
IDs through a local registry (`projects.json`) and **never** accepts an
arbitrary filesystem root, worktree path, or Claude session ID from a tool
call. Every modifying operation runs in an isolated detached Git worktree; the
production checkout is never edited directly.

```
                    OpenAI connector mTLS
                          ┌──────┐
   ChatGPT  ─── HTTPS ──▶│ ngrok │──▶ 127.0.0.1:8000  FastMCP server (this repo)
                          └──────┘                         │
                                                       Git worktrees
                                                       (isolated per job)
                                                           │
                                                    Claude Code (local)
```

> The project is also referred to as **BridgeLab** in its milestone records.
> The git remote is `anindyasundarbera/mcp-tool-bridge`.

---

## Why it exists

A raw `claude` CLI with `bypassPermissions` is unsafe to drive from a remote
assistant: it can run arbitrary Bash, edit any file, push, merge, and move
refs with no review. This bridge re-establishes a **governed boundary** between
"the assistant asks for work" and "the local machine does it":

- **Allowlisted projects only.** The local registry is the authorization source
  of truth. Unknown, disabled, or malformed projects fail closed.
- **Isolated worktrees.** Each development job gets a clean-baseline detached
  worktree beneath a private, bridge-owned root, outside every project.
- **Deterministic verification.** Only fixed, registry-configured command
  vectors run — no shell, no caller-supplied argv/env/cwd/timeout.
- **Human-gated commits.** A detached commit is created only after a *separate*
  human approval repeats the verification ID, HEAD, fingerprint, and ordered
  path list.
- **Bounded publication.** The one push surface is opt-in: a single verified
  detached commit to a *new, non-trunk* remote branch on `origin` — never a
  force-push, merge, tag, trunk update, or ref move.
- **Durable state.** Job, verification, review, publication, sync, checkpoint,
  and audit records persist to a crash-safe SQLite database, so a restart
  re-hydrates in-flight and completed work instead of wiping it.

---

## The governed development lifecycle

```
 develop in isolated worktree
   → review diff            (list_candidate_changes / read_candidate_file / run_independent_review)
   → verify fixed commands   (verify_development_job)        binds evidence → (HEAD, fingerprint, paths)
   → approve commit          (commit_verified_development_job)  separate human approval; detached commit only
   → publish (opt-in)        (push_detached_development_commit) one new non-trunk remote branch on origin
   → sync canonical (opt-in) (sync_canonical_checkout)         ff-only to origin/<default> after a merge
```

Each arrow is a **separate** MCP call with its own capability gate and its own
exact-match confirmation of the prior state. No single tool silently chains
develop → commit → push.

Job states: `queued` → `preparing_worktree` → `running` → optionally
`validating_candidate` → one of `completed` / `failed` / `timed_out` /
`cancelled`, plus the resumable `paused_budget_exhausted` /
`paused_interrupted` / `blocked_input_required` and the terminal
`failed_infrastructure` / `quarantined_provenance`.

---

## The MCP tool surface (32 tools)

Every tool takes an exact `project_id` (and an opaque `job_id` where relevant),
never a raw path or session ID. Capability gates are listed in
[docs/TOOLS.md](docs/TOOLS.md).

| Group | Tools |
|---|---|
| **Project / repo read** | `list_allowed_projects`, `get_project_status`, `list_project_markdown`, `search_project_markdown`, `read_project_markdown`, `read_project_file_range`, `get_markdown_outline`, `get_project_file_metadata` |
| **Markdown write** | `write_project_markdown` |
| **Development lifecycle** | `send_to_local_dev`, `get_development_job`, `resume_development_job`, `cancel_development_job` |
| **Candidate review** | `list_candidate_changes`, `read_candidate_file`, `record_candidate_review`, `run_independent_review`, `get_candidate_review` |
| **Verification & commit** | `verify_development_job`, `commit_verified_development_job` |
| **Publication & sync** | `push_detached_development_commit`, `sync_canonical_checkout`, `prepare_canonical_for_work` |
| **Evidence & PR** | `export_evidence_bundle`, `open_pull_request` |
| **Introspection (dormant)** | `list_development_jobs`, `get_effective_project_policy`, `resolve_candidate_canonicalization` |
| **Host runtime verification (dormant)** | `run_runtime_verification` |
| **Working-tree capture (dormant)** | `preflight_working_tree_capture`, `working_tree_candidate_capture` |
| **Disposable runtime action (dormant)** | `standalone_openhands_action` |

Tools marked *dormant* are off by default; a project opts in by listing the
matching capability in `projects.json`.

---

## Install

Requires Python ≥ 3.11 and `git` on PATH.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
cp projects.example.json projects.json
chmod 600 .env projects.json
```

- Put the real ngrok authtoken only in `.env`.
- Review `projects.json` before startup — only exact IDs and capabilities from
  that file are usable through MCP.
- Ensure `JOB_WORKTREE_ROOT` is **outside** every configured project root.

See [docs/GETTING-STARTED.md](docs/GETTING-STARTED.md) for a full end-to-end
walkthrough, including registering a new project.

## Run

```bash
./run.sh
```

`run.sh` activates `.venv`, validates `.env`, `projects.json`, and the mTLS CA,
then launches `server.py` (FastMCP on `MCP_HOST:MCP_PORT`, default
`127.0.0.1:8000`) and `tunnel.py` (ngrok) as child processes with prefixed logs
(`[server]` / `[tunnel]`) and clean shutdown on Ctrl+C. When `NGROK_DOMAIN` is
set in `.env`, the tunnel binds to that reserved domain for a stable connector
URL; otherwise ngrok issues an ephemeral URL that changes every run.

The connector URL is `https://<ngrok-domain>/mcp`. The ngrok endpoint is
protected by the OpenAI connector mTLS CA (`openai-connectors-mtls-ca.pem`);
the configured Claude launcher is never exposed directly — only the bridge
talks to it over a local process boundary.

The two halves can also run separately in two terminals:
```bash
python server.py   # or: .venv/bin/python server.py
python tunnel.py
```

## Test

Core tests have no network, model, ngrok, launcher, or real-repository
dependency:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

Once the server is running, a live smoke test lists tools, checks a project,
starts an isolated no-edit job, and polls its result:

```bash
.venv/bin/python test_client.py --project <project_id>
```

---

## Security model (summary)

- The local registry (`projects.json`) is the **only** authorization source of
  truth. Enforce authorization and validation in server code; never rely on
  model instructions for security.
- Tool callers provide stable `project_id`s and relative paths — never
  filesystem roots, raw session IDs, executables, argv, environment, cwd, or
  timeouts. Every command uses a fixed argv vector with `shell=True` forbidden.
- Reject hidden paths, protected paths, traversal, and symlink escapes.
- Claude Code may use `acceptEdits` only within its fixed project worktree;
  Bash, web, secondary MCP tools, slash commands, and `bypassPermissions` are
  disabled.
- No tool may silently commit, push, merge, tag, delete, restart production, or
  broaden its own permissions. The only push surface is opt-in
  `push_development_commit`; the only local-checkout update surface is opt-in
  `sync_canonical_checkout` (fast-forward only).
- `.env`, tokens, certificates, `.venv`, caches, and `projects.json` never enter
  Git or milestone archives.

See [docs/SECURITY.md](docs/SECURITY.md) for the full non-negotiable boundary
list and the threat model.

---

## Durable state

Job and verification records persist to a single local SQLite database so a
server restart no longer amnesiates in-flight or completed work:

```
JOB_WORKTREE_ROOT/.state/bridge.db        (file 0o600, dir 0o700)
```

The database uses WAL journaling with `synchronous=FULL` (each state transition
commits crash-safely). On startup every manager re-hydrates its records
synchronously before the event loop starts. Reconciliation is honest, not
magical: a non-terminal job that was in progress when the previous process died
is reloaded as `failed` (its asyncio task and Claude process are gone); a
non-terminal job **with** checkpoints reloads as `paused_interrupted`
(resumable). Unparseable rows are skipped (fail-closed); a database whose stored
schema version is newer than this code is refused on open. Run only one bridge
process per `JOB_WORKTREE_ROOT`. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
for the full schema and reconciliation rules.

---

## Configuration

Runtime knobs live in `.env` (see `.env.example` for the full list with
defaults). Project authorization lives in `projects.json` (see
`projects.example.json` and [docs/CAPABILITIES.md](docs/CAPABILITIES.md)). The
`mcp-servers.json` at the repo root is a convenience catalog of the MCP servers
used in this development environment (not consumed by the bridge itself).

### Configuring the Claude invocation

The bridge starts Claude Code via a configurable launch command (shell-split
with `shlex`). The default `CLAUDE_LAUNCH_COMMAND` runs `claude` under
`ollama launch`; to run the `claude` CLI directly instead, set:

```bash
CLAUDE_LAUNCH_COMMAND=claude --model {model} -p
```

The standalone `{model}` token is the substitution point — the bridge replaces
it with the resolved model (`project.claude_model` → `CLAUDE_MODEL`). Everything
up to and including the operator-owned `--` passthrough is yours; the bridge
always appends the **security tail** afterwards (`--permission-mode`,
`--tools`, `--disallowedTools Bash WebFetch WebSearch mcp__*`,
`--strict-mcp-config`, `--disable-slash-commands`, `--max-turns`,
`--output-format`, `--effort`) regardless of the launcher, so the governed
boundary cannot drift. A separate `CLAUDE_REVIEW_LAUNCH_COMMAND` governs the
one-shot read-only reviewer (defaults to the same prefix). Effort resolves as
per-call value → project `default_effort` → `CLAUDE_DEFAULT_EFFORT`.

| Setting | Default | Purpose |
|---|---:|---|
| `MCP_PORT` | 8000 | FastMCP HTTP listen port |
| `CANDIDATE_MCP_PORT` | 8001 | Self-managed candidate probe port (must differ) |
| `JOB_WORKTREE_ROOT` | `~/.local/state/local-dev-mcp/worktrees` | Private root for detached worktrees + `.state` |
| `CLAUDE_MODEL` | `glm-5.2:cloud` | Underlying model for Claude Code jobs |
| `CLAUDE_LAUNCH_COMMAND` | `ollama launch claude --model {model} --yes -- -p` | Drafter launch prefix (`{model}` substituted) |
| `CLAUDE_REVIEW_LAUNCH_COMMAND` | `ollama launch claude --model {model} --yes -- -p` | Reviewer launch prefix |
| `CLAUDE_DEFAULT_EFFORT` | `high` | Global default effort when call and project omit one |
| `CLAUDE_TIMEOUT_SECONDS` | 1800 | Maximum Claude run time |
| `CLAUDE_HARD_TURNS` | 120 | Absolute cumulative turn cap |
| `MAX_INPUT_CHARS` | 100,000 | Maximum job instruction length |
| `READER_INJECTION_DEFENSE` | off | Fence + delimit untrusted repo content the reader ingests |

The complete limit table (verification, runtime verification, working-tree
capture, Markdown I/O, budgets) is in [docs/GETTING-STARTED.md](docs/GETTING-STARTED.md#limits).

---

## Documentation

Authoritative, newcomer-oriented docs live in [`docs/`](docs/README.md):

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — components, request flow, lifecycle, durable state, module map, threat model
- [docs/TOOLS.md](docs/TOOLS.md) — full 32-tool reference
- [docs/CAPABILITIES.md](docs/CAPABILITIES.md) — capability model and `projects.json` schema
- [docs/GETTING-STARTED.md](docs/GETTING-STARTED.md) — install, configure a project, run, test end-to-end
- [docs/SECURITY.md](docs/SECURITY.md) — security boundaries and non-negotiable rules
- [docs/README.md](docs/README.md) — index of the milestone decision records and briefs

`AGENTS.md` is the entry point for AI agents working on this repository,
including the rule that **any change after a capability addition must be
committed**.

> `docs/exploratory-rag/` holds two archived notes
> (`DEVELOPMENT_GUIDE.md`, `MANUAL_INSTALLATION_GUIDE.md`) from a separate,
> exploratory RAG-augmentation effort (LightRAG / RAG-Anything / tokengate).
> They are **not** documentation for the bridge itself; they were relocated
> from the repo root so the root stays authoritative for the bridge.

---

## Project layout

```
server.py, tunnel.py, run.sh       entry points (server / ngrok tunnel / launcher)
src/local_dev_mcp/                 the bridge (26 modules, ~15k lines)
  server.py                        FastMCP tool surface (32 tools)
  registry.py, models.py           project registry + capability/policy models
  config.py                        Settings (env-driven)
  jobs.py, claude.py                development-job lifecycle + Claude executor
  verification.py, review.py        verify / review / commit
  pr.py, sync.py, evidence.py      publication, canonical sync, evidence bundle
  runtime_verification.py           governed host-runtime checks (MCPB-011)
  working_tree_capture.py           dirty-tree candidate capture (MCPB-012)
  open_hands_action.py              governed disposable runtime action
  introspection.py                  read-only introspection (M-AL-01)
  store.py                          durable SQLite state (20 tables)
  reader.py, writer.py             bounded Markdown read/write
  git.py, worktrees.py, candidate.py   git helpers / worktree mgmt
  authorization.py, requirement.py  durable authorizations / requirement traceability
tests/                             340+ unittest tests, no network/model deps
docs/                              milestone decision records + this documentation set
```

The `LightRAG/`, `RAG-Anything/`, and `tokengate/` directories are vendored
clones related to the exploratory RAG work noted above; they are not part of
the bridge and are not tracked by this repository.