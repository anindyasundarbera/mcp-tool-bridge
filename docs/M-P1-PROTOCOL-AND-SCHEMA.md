# M-P1 — Protocol Support Matrix & Public MCP Schema Compatibility Policy

This document defines the authoritative protocol support matrix, transport bindings,
client lifecycle contract, and public MCP tool schema compatibility policy for the
`local-dev-mcp` (BridgeLab) service.

---

## 1. A1.1.1 Protocol & Transport Support Matrix

The MCP Tool Bridge acts as a governed middleware boundary connecting AI model clients
(such as ChatGPT or Claude Code) to local development repositories and execution environments.

### 1.1 Specification & Runtime Compatibility

| Component | Version / Requirement | Notes |
|---|---|---|
| **Python Runtime** | `>=3.11` | Tested through Python 3.14. Strongly typed (`mypy --strict` compliant). |
| **FastMCP** | `>=3.4, <4.0` | High-level MCP server framework (`fastmcp.FastMCP` and `fastmcp.Client`). |
| **MCP SDK** | `mcp` (`>=1.0.0`) | Model Context Protocol official specification primitives. |
| **Pydantic** | `^2.10` | Parameter validation, model serialization, and JSON schema generation. |
| **SQLite3** | Durable state | Schema v1 with WAL journaling for concurrent multi-manager state storage. |

### 1.2 Transports

The bridge supports two transport bindings:

1. **Standard I/O (`stdio`)**:
   - Primarily used when invoked as a local subprocess by Claude Desktop, Claude Code, or local MCP CLI harnesses.
   - Messages are framed as JSON-RPC 2.0 lines across stdin/stdout. Stderr is reserved exclusively for diagnostics and logging.
2. **HTTP Stream (`http`)**:
   - Production entrypoint (`server.run(transport="http", host=..., port=...)`).
   - Serves Server-Sent Events (SSE) and HTTP streaming endpoints for remote model connectors.

### 1.3 Remote Connection & Security Boundary (ngrok / mTLS)

For remote model access (e.g. OpenAI ChatGPT custom actions):
- The public ingress is terminated at an **ngrok** tunnel.
- **OpenAI Connector mTLS**: Mutual TLS certificates protect the endpoint. Only authorized OpenAI client certificates are accepted at the edge.
- Only the MCP server HTTP port is exposed. The local Claude launcher, bash shells, and filesystem roots are never exposed directly to the network.

### 1.4 Client Lifecycle Guarantees

Clients connecting via `fastmcp.Client` experience deterministic lifecycle phases:
1. **Handshake & Protocol Initialization**:
   - Client establishes connection; server advertises capabilities and schema version.
2. **Tool Enumeration (`list_tools`)**:
   - Returns all 39 registered tools along with parameter JSON schemas and exact `ToolAnnotations`.
3. **Execution & Error Handling**:
   - Invocations are validated against the local project registry (`projects.json`) and capability policies.
   - Denied or invalid calls fail closed with standard `ToolError` envelopes without leaking sensitive filesystem paths.
4. **Session Disconnection & Reconnection**:
   - Stateless transport handling allows clients to disconnect and reconnect across distinct sessions without state corruption.
   - Multiple concurrent read-only queries (e.g. via `asyncio.gather`) execute safely without locks or deadlocks.
5. **Clean Termination**:
   - Server lifecycle exposes `server.close()` / `server.close_managers()` to close SQLite WAL connections and release isolated worktree locks cleanly.

---

## 2. A1.2.2 Public MCP Schema Compatibility Policy

The bridge maintains a public tool catalog covering all **39 registered MCP tools**.
Schema stability is guaranteed through programmatic drift verification.

### 2.1 Lifecycle Stages

Tools in `PUBLIC_TOOL_CATALOG` (`src/local_dev_mcp/schema_compat.py`) are categorized into three stages:

```
PUBLIC_TOOL_CATALOG (39 Tools)
├── CORE_V1 (31 tools)
│   ├── Governance & Introspection (list_allowed_projects, get_effective_project_policy, ...)
│   ├── Job Lifecycle (send_to_local_dev, get_development_job, resume, pause, cancel, ...)
│   ├── Verification & Commit (verify_development_job, commit_verified_development_job, ...)
│   ├── Review (record_candidate_review, list_candidate_changes, read_candidate_file, ...)
│   └── Capture & Evidence (preflight_working_tree_capture, working_tree_candidate_capture, ...)
├── SECONDARY_V1 (7 tools)
│   ├── Markdown Tools (list_project_markdown, read_project_markdown, search, outline, write)
│   └── Remote Integrations (open_pull_request, sync_canonical_checkout)
└── QUARANTINED_PRE_V1 (1 tool)
    └── standalone_openhands_action
```

1. **`core_v1` (31 Tools)**:
   - Primary governance, worktree lifecycle, verification, commit, review, and introspection surface.
   - **Stability**: Frozen beta contract. Zero breaking changes permitted without major milestone review.
2. **`secondary_v1` (7 Tools)**:
   - File-level Markdown authoring and auxiliary repository sync / PR operations.
   - Subject to strict write roots (`markdown_write_roots`) and opt-in project capabilities.
3. **`quarantined_pre_v1` (1 Tool)**:
   - `standalone_openhands_action`: Restricted disposable runtime execution.
   - Quarantined behind `governed_disposable_runtime_action_execution` capability. Marked pre-v1; subject to breaking change or deprecation prior to v1.0.

### 2.2 ToolAnnotations Contract

Every public tool declares four explicit boolean hints in `ToolAnnotations`:

- **`readOnlyHint`**: `True` if the tool strictly inspects state without modifying git history, files, or job records. (e.g. `list_allowed_projects`, `get_project_status`, `export_evidence_bundle`).
- **`destructiveHint`**: `True` if the tool executes irreversible git mutations, commits, worktree cancellations, or branch publications (e.g. `commit_verified_development_job`, `cancel_development_job`, `push_detached_development_commit`).
- **`idempotentHint`**: `True` if repeated invocations with identical parameters produce identical end states without cumulative side effects (e.g. `cancel_development_job`, `export_evidence_bundle`, `discard_candidate`).
- **`openWorldHint`**: `True` if the tool interacts with host runtime processes, external networks, remotes, or non-deterministic agent executors (e.g. `send_to_local_dev`, `verify_development_job`, `open_pull_request`, `run_runtime_verification`).

### 2.3 Pre-1.0 Deprecation & Evolution Policy

1. **Additive Schema Evolution**:
   - New parameters to existing tools MUST be optional (`default=None` or sensible safe default).
   - Existing parameter types and validation patterns (such as 32-character hexadecimal job IDs or 40-character commit SHAs) must never be narrowed or removed in minor updates.
2. **Deprecation Notice Period**:
   - Any tool designated for deprecation will first transition to a deprecated notice in its description and release notes for at least one minor release before retirement.
3. **Quarantine Isolation**:
   - Quarantined tools remain disabled for all standard projects unless the operator explicitly grants the corresponding capability in `projects.json`.

### 2.4 Deterministic Schema Digest & Automated Drift Verification

To prevent accidental drift between codebase changes and the published schema:
- **`compute_schema_digest()`** computes a canonical SHA-256 fingerprint over sorted tool names, JSON parameter schemas, and `ToolAnnotations`.
- **`validate_server_schema_compatibility()`** automatically verifies:
  1. No expected tools from `PUBLIC_TOOL_CATALOG` are missing.
  2. No unregistered or unreviewed tools are exposed.
  3. Every tool's runtime `ToolAnnotations` match catalog expectations.
- Regression tests in `tests/test_schema_compat.py` enforce these checks on every test run.

---

## 3. Verification & Safety Invariants

### 3.1 Repository Immutability Guarantee

All read-only queries and rejected negative operations (such as unauthorized project access, disabled capability invocation, or path traversal attempts) guarantee that the canonical repository remains completely untouched:
- `git status --short` remains strictly empty (`""`).
- `git rev-parse HEAD` remains strictly identical to the initial commit SHA.

Verified in `tests/test_mcp_client_lifecycle.py`.
