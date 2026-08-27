# M-CC-11 — Security hardening (tail)

## Decision

M-CC-11 ships four **opt-in, per-project hardening mechanisms** for the core
BridgeLab loop. All are internal — the milestone introduces **no new MCP tools**
(tool count stays 32), no new push surface beyond the existing opt-in
`push_development_commit` / `open_pull_request` path, and no change to the
additive SQLite schema (`SCHEMA_VERSION` stays 1, only new tables via
`CREATE TABLE IF NOT EXISTS`). Every mechanism defaults off (`None`/`False`), so
no existing project is affected until it opts in.

1. **`filesystem_policy`** — per-project read/write allowlists enforced in the
   reader (`resolve_project_path`, `list_markdown`, `search`) and writer
   (`_validate_target`) surfaces. Matching is prefix-based at a directory
   boundary (`docs` never allows `docs2/…`); a trailing `**` glob is honored
   only as the final segment. Intersects with (never widens) the existing
   protected-path and markdown-write-root rules.
2. **`credential_policy`** — least-privilege, short-lived credential projection.
   The worktree subprocess environment is a **deterministic minimal base** plus
   ONLY the credential env names the project declares — never a copy of the
   bridge's whole environment. Applied to the Claude worktree launcher
   (`claude.py`) and the verification/supply-chain runner (`verification.py`).
   Credential names that would break the M-CC-09 hermetic lab (`TZ`,
   `SOURCE_DATE_EPOCH`, `PYTHONHASHSEED`, …) and interpreter/loader rebinds
   (`PYTHONPATH`, `PYTHONHOME`, …) are rejected at validation.
3. **`signed_manifests`** — deterministic HMAC-SHA256 signing of the evidence
   bundle's content address with a project key
   (`BRIDGELAB_EVIDENCE_SIGNING_KEY_<PROJECT_ID_UPPER>` env var, fallback
   `BRIDGELAB_EVIDENCE_SIGNING_KEY`, or `<state>/signing_keys/<project_id>.key`).
   The manifest (`algorithm`, `project_id`, `bundle_sha256`, `signature`) is
   attached to the exported bundle and persisted in a new `evidence_manifests`
   table. **Fail closed**: a project that opts in but has no configured key is
   an `EvidenceBundleError`, never a silently unsigned bundle. The digest is
   recomputable by stripping both `bundle_sha256` and `manifest`.
4. **`sensitivity_policy`** — classify artifact sensitivity before export.
   Declared sensitive paths are normalized to directory prefixes and extend the
   built-in sensitive-path filter in `export_bundle`, redacting the sensitive
   `changed_paths` and scrubbing their diff-stat lines, with a
   `sensitive_excluded_count` surfaced in the bundle plus a `sensitivity`
   section recording the applied policy.

The existing M-CC-11 supply-chain gate (severity-gated scanners that must exit
0 or the commit is rejected, fail-closed on missing scanners) was already in
place pre-commit; this milestone adds the `credential_policy` projection into
that scanner runner and proves end-to-end that a projected credential reaches a
real scanner subprocess.

`validate_policy` enforces capability dependencies (matching the existing rule
style): `signed_manifests` and `sensitivity_policy` both require
`export_evidence_bundle`.

## Files changed

- `src/local_dev_mcp/models.py` — `FileSystemPolicy`, `CredentialPolicy`,
  `SensitivityPolicy`; three `ProjectPolicy` fields + `signed_manifests`;
  `validate_policy` capability-dependency rules.
- `src/local_dev_mcp/security.py` (**new**) — allowlist matching, credential
  projection, signing-key resolution, sign/verify, sensitivity-prefix reduction.
- `src/local_dev_mcp/evidence.py` — sensitivity policy extends exclude/scrub;
  signed bundles persisted and returned.
- `src/local_dev_mcp/store.py` — additive `evidence_manifests` table +
  `insert_evidence_manifest` / `load_evidence_manifests`.
- `src/local_dev_mcp/reader.py` — read-allowlist enforcement.
- `src/local_dev_mcp/writer.py` — write-allowlist enforcement.
- `src/local_dev_mcp/claude.py` — credential projection into the Claude worktree
  launcher.
- `src/local_dev_mcp/verification.py` — credential projection into verification
  commands and supply-chain scanners.
- `src/local_dev_mcp/introspection.py` — `effective_policy_view` exposes the new
  policies.
- `tests/test_security.py` (**new**, 16 tests) — see below.
- `pyproject.toml` — `0.10.1` → `0.11.0` (capability addition; `__init__.py` is
  stale at `0.8.0` and intentionally not kept in sync).

## Tests

`tests/test_security.py` (16 tests, fake executors, no network):

- **Policy validation** — all four mechanisms opt-in by default; allowlist /
  credential-name / sensitivity-path shape rejection; deterministic-env and
  loader-rebind credential names rejected; `signed_manifests` / `sensitivity_policy`
  require `export_evidence_bundle`; `effective_policy_view` exposes them.
- **Filesystem policy** — read allowlist permits `docs/**` and blocks `src/…`
  (reader `read_markdown` raises, `list_markdown` omits); write allowlist
  intersects with approved write roots (`docs` allowed, `canonical` blocked
  despite being a write root); prefix boundary (`docs` ≠ `docs2`).
- **Credential projection** — unit: only declared names copied, missing vars
  skipped, unset policy passes base unchanged; integration: a real scanner
  subprocess succeeds only when `GH_TOKEN` is projected via `credential_policy`,
  and the same scanner **rejects the commit** when no policy allows it (proof
  that the bridge environment does not leak into the worktree).
- **Sensitivity classification** — end-to-end bundle: two injected
  `config/internal/**` paths redacted with `excluded_count == 2`, sensitive
  strings absent from the serialized bundle, content address recomputable; unit:
  prefix matching and diff-detail scrubbing.
- **Signed manifests** — end-to-end export signs, manifest verifies (wrong key
  fails), persisted via `store.load_evidence_manifests`, digest recomputable by
  stripping `bundle_sha256` + `manifest`; unit round-trip with tamper detection;
  fail-closed `EvidenceBundleError` when no key is configured.

## Status

Implemented and verified locally. Full core suite: **457 tests, all passing**
(441 pre-existing + 16 new). Committed on `cap/m-cc11-security`. Not pushed, no
PR opened, no live BridgeLab restart.
