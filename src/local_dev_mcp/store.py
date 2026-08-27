"""Durable job and verification state for the local development bridge.

State lives in a single SQLite database beneath the private, bridge-owned
``.state`` directory under the configured job worktree root. The database is
opened with WAL journaling and ``synchronous=FULL`` so each state transition
commits durably; a server restart can then re-hydrate every job and verification
record from disk instead of amnesiating them.

This module is the only place that knows the on-disk schema. It speaks in plain
``dict`` rows; the managers own the object<->row mapping (see ``jobs.py`` and
``verification.py``). This keeps the dependency direction one-way
(``jobs``/``verification`` import ``store``; ``store`` imports only
``worktrees``) and avoids an import cycle.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .worktrees import _ensure_private_directory

SCHEMA_VERSION = 1

_CREATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    effort TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    cancel_requested INTEGER NOT NULL,
    session_id TEXT,
    committed_sha TEXT,
    worktree_path TEXT,
    worktree_base_head TEXT,
    result_json TEXT,
    candidate_json TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS verifications (
    verification_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    base_head TEXT NOT NULL,
    diff_fingerprint TEXT NOT NULL,
    paths_json TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    used INTEGER NOT NULL,
    commands_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    base_head TEXT NOT NULL,
    diff_fingerprint TEXT NOT NULL,
    paths_json TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    reviewer_id TEXT,
    verdict_json TEXT NOT NULL,
    used INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
    project_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    remote_url TEXT NOT NULL,
    published_at TEXT NOT NULL,
    PRIMARY KEY (project_id, job_id)
);
CREATE TABLE IF NOT EXISTS syncs (
    sync_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    from_head TEXT NOT NULL,
    to_head TEXT NOT NULL,
    branch TEXT NOT NULL,
    remote_url TEXT NOT NULL,
    idempotent INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS job_checkpoints (
    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    seq INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    changed_paths_json TEXT NOT NULL,
    partial_fingerprint TEXT,
    tokens_json TEXT,
    event_json TEXT,
    milestone TEXT
);
CREATE TABLE IF NOT EXISTS job_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_progress (
    job_id TEXT PRIMARY KEY,
    pause_reason TEXT,
    turns_used INTEGER NOT NULL,
    tokens_used INTEGER NOT NULL,
    extensions_granted INTEGER NOT NULL,
    no_progress_turns INTEGER NOT NULL,
    tool_failures INTEGER NOT NULL,
    soft_warned INTEGER NOT NULL,
    milestones_json TEXT,
    parent_job_id TEXT,
    delegation_mandate_json TEXT,
    drafter_id TEXT,
    successor_base_head TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delegations (
    delegation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_job_id TEXT NOT NULL,
    child_job_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    mandate_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ratifications (
    ratification_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    candidate_fingerprint TEXT NOT NULL,
    base_head TEXT NOT NULL,
    actor TEXT NOT NULL,
    authority_source TEXT NOT NULL,
    ratified_at TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requirements (
    requirement_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    text TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_line INTEGER NOT NULL,
    normative BOOLEAN NOT NULL
);
CREATE TABLE IF NOT EXISTS requirement_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    requirement_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    link_type TEXT NOT NULL,
    link_value TEXT NOT NULL,
    FOREIGN KEY (requirement_id) REFERENCES requirements(requirement_id),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);
CREATE TABLE IF NOT EXISTS authorizations (
    authorization_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    job_id TEXT,
    actor TEXT NOT NULL,
    authority_source TEXT NOT NULL,
    capability TEXT NOT NULL,
    target TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT,
    single_use INTEGER NOT NULL DEFAULT 1,
    used INTEGER NOT NULL DEFAULT 0,
    delegated_from TEXT,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_classifications (
    classification TEXT PRIMARY KEY,
    description TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS authority_records (
    authority_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    event_id INTEGER,
    actor TEXT NOT NULL,
    authority_source TEXT NOT NULL,
    scope TEXT NOT NULL,
    classification TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supply_chain_scans (
    scan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    verification_id TEXT NOT NULL,
    scan_command TEXT NOT NULL,
    scan_argv TEXT NOT NULL,
    exit_code INTEGER,
    passed INTEGER NOT NULL,
    scanned_at TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_manifests (
    manifest_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    bundle_sha256 TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    signature TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_verifications (
    runtime_verification_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    check_id TEXT NOT NULL,
    expected_head TEXT NOT NULL,
    verified_head TEXT NOT NULL,
    status TEXT NOT NULL,
    exit_code INTEGER,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    stdout_json TEXT NOT NULL,
    stderr_json TEXT NOT NULL,
    stdout_hash TEXT NOT NULL,
    stderr_hash TEXT NOT NULL,
    policy_fingerprint TEXT NOT NULL,
    evidence_fingerprint TEXT NOT NULL,
    repository_json TEXT NOT NULL,
    verified_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS working_tree_captures (
    capture_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    base_head TEXT NOT NULL,
    working_tree_fingerprint TEXT NOT NULL,
    candidate_fingerprint TEXT NOT NULL,
    include_paths_json TEXT NOT NULL,
    protected_exclude_paths_json TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    protected_exclude_manifest_json TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    captured_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oh_ledger (
    ledger_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL,
    canonical_baseline_fingerprint TEXT NOT NULL,
    disposable_candidate_fingerprint TEXT NOT NULL,
    session_id TEXT NOT NULL,
    permitted_tools TEXT NOT NULL,
    observed_tool_usage TEXT NOT NULL,
    command_results_json TEXT NOT NULL,
    inspection_result TEXT NOT NULL,
    canonical_verification_result TEXT NOT NULL,
    teardown_result TEXT NOT NULL,
    residue_verification_result TEXT NOT NULL,
    evidence_bundle_id TEXT NOT NULL,
    evidence_fingerprint TEXT NOT NULL,
    final_verdict TEXT NOT NULL,
    failure_reason TEXT,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_checkpoint_detail (
    checkpoint_id INTEGER PRIMARY KEY,
    completed_steps_json TEXT,
    remaining_steps_json TEXT,
    tests_json TEXT,
    decisions_json TEXT,
    tool_history_json TEXT,
    FOREIGN KEY (checkpoint_id) REFERENCES job_checkpoints(checkpoint_id)
      ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS verification_environment (
    verification_id TEXT PRIMARY KEY,
    environment_digest TEXT NOT NULL,
    FOREIGN KEY (verification_id) REFERENCES verifications(verification_id)
      ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS verification_bindings (
    verification_id TEXT PRIMARY KEY,
    toolchain_digest TEXT,
    reviewer_independence_class TEXT,
    expires_at TEXT,
    authority_ref TEXT,
    FOREIGN KEY (verification_id) REFERENCES verifications(verification_id)
      ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS review_bindings (
    review_id TEXT PRIMARY KEY,
    toolchain_digest TEXT,
    reviewer_independence_class TEXT,
    expires_at TEXT,
    authority_ref TEXT,
    FOREIGN KEY (review_id) REFERENCES reviews(review_id)
      ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS review_content (
    review_id TEXT PRIMARY KEY,
    verdict_hash TEXT NOT NULL,
    FOREIGN KEY (review_id) REFERENCES reviews(review_id)
      ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS revision_resolves_finding (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id TEXT NOT NULL,
    resolving_job_id TEXT NOT NULL,
    resolving_checkpoint_id INTEGER,
    finding_ref TEXT,
    resolved_at TEXT NOT NULL,
    FOREIGN KEY (review_id) REFERENCES reviews(review_id)
      ON DELETE CASCADE,
    FOREIGN KEY (resolving_checkpoint_id) REFERENCES job_checkpoints(checkpoint_id)
      ON DELETE CASCADE
);
-- M-EV-08: additive index backing the time-based GC delete in
-- ``gc_artifacts`` (retention window scan over ``recorded_at``). Additive
-- only; ``SCHEMA_VERSION`` stays 1.
CREATE INDEX IF NOT EXISTS idx_job_checkpoints_recorded_at
    ON job_checkpoints(recorded_at);
-- M-GV-03: durable registration ledger for external apex grounding. Records
-- the ``apex_authority_ref`` a project registered with so the registry and the
-- genesis-job path can reject a ref that changed without re-confirmation.
-- Additive ``CREATE TABLE IF NOT EXISTS`` only; ``SCHEMA_VERSION`` stays 1.
CREATE TABLE IF NOT EXISTS registered_apex_authority (
    project_id TEXT PRIMARY KEY,
    apex_authority_ref TEXT NOT NULL,
    confirmed_at TEXT NOT NULL
);
-- M-CC-10: additive sidecar persisting a job's worktree ``base_head`` so a
-- lost worktree can be reconstructed from the content-addressed checkpoint
-- ledger even after a server restart. The ``jobs`` row nulls
-- ``worktree_base_head`` when the directory vanishes (see ``_reload_jobs``),
-- so this sidecar is the durable authority for the base commit. Additive only;
-- ``SCHEMA_VERSION`` stays 1.
CREATE TABLE IF NOT EXISTS job_worktree_meta (
    job_id TEXT PRIMARY KEY,
    base_head TEXT NOT NULL,
    worktree_path TEXT,
    recorded_at TEXT NOT NULL
);
"""

_UPSERT_JOB = """
INSERT INTO jobs (
    job_id, project_id, effort, status, attempt, created_at, updated_at,
    cancel_requested, session_id, committed_sha, worktree_path,
    worktree_base_head, result_json, candidate_json, error
) VALUES (
    :job_id, :project_id, :effort, :status, :attempt, :created_at, :updated_at,
    :cancel_requested, :session_id, :committed_sha, :worktree_path,
    :worktree_base_head, :result_json, :candidate_json, :error
)
ON CONFLICT(job_id) DO UPDATE SET
    project_id = excluded.project_id,
    effort = excluded.effort,
    status = excluded.status,
    attempt = excluded.attempt,
    created_at = excluded.created_at,
    updated_at = excluded.updated_at,
    cancel_requested = excluded.cancel_requested,
    session_id = excluded.session_id,
    committed_sha = excluded.committed_sha,
    worktree_path = excluded.worktree_path,
    worktree_base_head = excluded.worktree_base_head,
    result_json = excluded.result_json,
    candidate_json = excluded.candidate_json,
    error = excluded.error
"""

_UPSERT_RECORD = """
INSERT INTO verifications (
    verification_id, project_id, job_id, base_head, diff_fingerprint,
    paths_json, verified_at, used, commands_json
) VALUES (
    :verification_id, :project_id, :job_id, :base_head, :diff_fingerprint,
    :paths_json, :verified_at, :used, :commands_json
)
ON CONFLICT(verification_id) DO UPDATE SET
    project_id = excluded.project_id,
    job_id = excluded.job_id,
    base_head = excluded.base_head,
    diff_fingerprint = excluded.diff_fingerprint,
    paths_json = excluded.paths_json,
    verified_at = excluded.verified_at,
    used = excluded.used,
    commands_json = excluded.commands_json
"""

_UPSERT_REVIEW = """
INSERT INTO reviews (
    review_id, project_id, job_id, base_head, diff_fingerprint,
    paths_json, reviewed_at, reviewer_id, verdict_json, used
) VALUES (
    :review_id, :project_id, :job_id, :base_head, :diff_fingerprint,
    :paths_json, :reviewed_at, :reviewer_id, :verdict_json, :used
)
ON CONFLICT(review_id) DO UPDATE SET
    project_id = excluded.project_id,
    job_id = excluded.job_id,
    base_head = excluded.base_head,
    diff_fingerprint = excluded.diff_fingerprint,
    paths_json = excluded.paths_json,
    reviewed_at = excluded.reviewed_at,
    reviewer_id = excluded.reviewer_id,
    verdict_json = excluded.verdict_json,
    used = excluded.used
"""

_UPSERT_PUBLICATION = """
INSERT INTO publications (
    project_id, job_id, commit_sha, branch_name, remote_url, published_at
) VALUES (
    :project_id, :job_id, :commit_sha, :branch_name, :remote_url, :published_at
)
ON CONFLICT(project_id, job_id) DO UPDATE SET
    commit_sha = excluded.commit_sha,
    branch_name = excluded.branch_name,
    remote_url = excluded.remote_url,
    published_at = excluded.published_at
"""

_INSERT_SYNC = """
INSERT INTO syncs (
    project_id, synced_at, from_head, to_head, branch, remote_url, idempotent
) VALUES (
    :project_id, :synced_at, :from_head, :to_head, :branch, :remote_url,
    :idempotent
)
"""

_INSERT_CHECKPOINT = """
INSERT INTO job_checkpoints (
    job_id, project_id, kind, seq, recorded_at, changed_paths_json,
    partial_fingerprint, tokens_json, event_json, milestone
) VALUES (
    :job_id, :project_id, :kind, :seq, :recorded_at, :changed_paths_json,
    :partial_fingerprint, :tokens_json, :event_json, :milestone
)
"""

_INSERT_EVENT = """
INSERT INTO job_events (
    job_id, project_id, occurred_at, event_type, detail_json
) VALUES (
    :job_id, :project_id, :occurred_at, :event_type, :detail_json
)
"""

_UPSERT_JOB_WORKTREE_META = """
INSERT INTO job_worktree_meta (
    job_id, base_head, worktree_path, recorded_at
) VALUES (
    :job_id, :base_head, :worktree_path, :recorded_at
)
ON CONFLICT(job_id) DO UPDATE SET
    base_head = excluded.base_head,
    worktree_path = excluded.worktree_path,
    recorded_at = excluded.recorded_at
"""

_UPSERT_PROGRESS = """
INSERT INTO job_progress (
    job_id, pause_reason, turns_used, tokens_used, extensions_granted,
    no_progress_turns, tool_failures, soft_warned, wall_seconds_used,
    milestones_json, parent_job_id, delegation_mandate_json, drafter_id,
    successor_base_head, updated_at
) VALUES (
    :job_id, :pause_reason, :turns_used, :tokens_used, :extensions_granted,
    :no_progress_turns, :tool_failures, :soft_warned, :wall_seconds_used,
    :milestones_json, :parent_job_id, :delegation_mandate_json, :drafter_id,
    :successor_base_head, :updated_at
)
ON CONFLICT(job_id) DO UPDATE SET
    pause_reason = excluded.pause_reason,
    turns_used = excluded.turns_used,
    tokens_used = excluded.tokens_used,
    extensions_granted = excluded.extensions_granted,
    no_progress_turns = excluded.no_progress_turns,
    tool_failures = excluded.tool_failures,
    soft_warned = excluded.soft_warned,
    wall_seconds_used = excluded.wall_seconds_used,
    milestones_json = excluded.milestones_json,
    parent_job_id = excluded.parent_job_id,
    delegation_mandate_json = excluded.delegation_mandate_json,
    drafter_id = excluded.drafter_id,
    successor_base_head = excluded.successor_base_head,
    updated_at = excluded.updated_at
"""

_UPSERT_REQUIREMENT = """
INSERT INTO requirements (
    requirement_id, project_id, text, source_path, source_line, normative,
    kind, status, deferred_authority, updated_at
) VALUES (
    :requirement_id, :project_id, :text, :source_path, :source_line, :normative,
    :kind, :status, :deferred_authority, :updated_at
)
ON CONFLICT(requirement_id) DO UPDATE SET
    project_id = excluded.project_id,
    text = excluded.text,
    source_path = excluded.source_path,
    source_line = excluded.source_line,
    normative = excluded.normative,
    kind = COALESCE(excluded.kind, requirements.kind),
    status = COALESCE(excluded.status, requirements.status),
    deferred_authority = COALESCE(excluded.deferred_authority, requirements.deferred_authority),
    updated_at = COALESCE(excluded.updated_at, requirements.updated_at)
"""

_UPSERT_REQUIREMENT_LINK = """
INSERT INTO requirement_links (
    requirement_id, job_id, link_type, link_value,
    artifact_kind, artifact_path, evidence_json, linked_at
) VALUES (
    :requirement_id, :job_id, :link_type, :link_value,
    :artifact_kind, :artifact_path, :evidence_json, :linked_at
)
"""


class StateStoreError(RuntimeError):
    """Raised when the durable state store cannot be opened or is unsupported."""


def ensure_state_dir(state_root: Path) -> Path:
    """Create and validate the private ``.state`` directory, returning it."""
    if not state_root.is_absolute():
        raise StateStoreError("state root must be absolute")
    return _ensure_private_directory(state_root)


# Additive columns added to existing tables across releases. ``CREATE TABLE IF
# NOT EXISTS`` is a no-op on an existing table, so these must be added via
# idempotent ``ALTER TABLE ... ADD COLUMN`` guarded by ``PRAGMA table_info``.
# SCHEMA_VERSION stays 1: the columns are additive and never break old rows.
_ADDITIVE_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "job_progress": (
        # ``tool_failures`` was part of the MCPB-010 schema from the start but
        # was never registered here, so a ``job_progress`` table created by an
        # earlier build (e.g. the pre-delegation schema) was never upgraded and
        # the progress upsert failed with "no column named tool_failures".
        # ``INTEGER NOT NULL`` needs a default for ``ALTER TABLE ... ADD COLUMN``.
        ("tool_failures", "INTEGER NOT NULL DEFAULT 0"),
        ("parent_job_id", "TEXT"),
        ("delegation_mandate_json", "TEXT"),
        # M-GV-05: identity of the agent that drafted the candidate, so a
        # later review can reject reviewer_id == drafter (self-certification).
        ("drafter_id", "TEXT"),
        # M-CT-02: cumulative wall-time across all slices (seconds). REAL so a
        # float accumulates precisely; NOT NULL DEFAULT 0 keeps old rows valid.
        ("wall_seconds_used", "REAL NOT NULL DEFAULT 0"),
        # M-CC-03: the pinned base commit of a successor job (the parent job's
        # published committed SHA). Additive TEXT column; NULL for non-successor
        # jobs, so existing rows are untouched and SCHEMA_VERSION stays 1.
        ("successor_base_head", "TEXT"),
    ),
    # M-EV-01: additive columns on the requirements + requirement_links tables
    # so the full matrix semantics (kind, status, deferred_authority,
    # artifact_kind, artifact_path, evidence_json, linked_at) are persisted
    # without altering the existing column set or bumping SCHEMA_VERSION.
    "requirements": (
        ("kind", "TEXT"),  # normative|example|commentary
        ("status", "TEXT"),  # implemented|unimplemented|deferred
        ("deferred_authority", "TEXT"),
        ("updated_at", "TEXT"),
    ),
    "requirement_links": (
        ("artifact_kind", "TEXT"),  # code|test|spec
        ("artifact_path", "TEXT"),
        ("evidence_json", "TEXT"),
        ("linked_at", "TEXT"),
    ),
}


def _migrate_additive_columns(connection: sqlite3.Connection) -> None:
    """Add any missing additive columns to existing tables (idempotent)."""
    for table, columns in _ADDITIVE_COLUMNS.items():
        existing = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, definition in columns:
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                )


def open_state_db(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the durable state database.

    WAL journaling with ``synchronous=FULL`` makes each committed state
    transition crash-safe. The schema is created idempotently. If the stored
    schema version is newer than this code supports, opening fails closed so
    the operator notices rather than silently losing data.
    """
    connection = sqlite3.connect(db_path, check_same_thread=False)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA busy_timeout=2000")
        connection.executescript(_CREATE_SCHEMA)
        _migrate_additive_columns(connection)
        _seed_source_classifications(connection)
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('version', ?) "
            "ON CONFLICT(key) DO NOTHING",
            (str(SCHEMA_VERSION),),
        )
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone()
        if row is None:
            raise StateStoreError("schema_meta is missing a version row")
        try:
            stored = int(row["value"])
        except ValueError as exc:
            raise StateStoreError("schema_meta version is not an integer") from exc
        if stored > SCHEMA_VERSION:
            raise StateStoreError(
                f"state database schema version {stored} is newer than supported "
                f"{SCHEMA_VERSION}; refusing to start to avoid data loss"
            )
        # Advance a stale stored version up to this code's SCHEMA_VERSION. The
        # insert above uses ON CONFLICT DO NOTHING, so an existing row from an
        # older schema never gets rewritten — without this an upgrade would
        # silently leave the stored version pinned below SCHEMA_VERSION and a
        # later downgrade would not be detected by the ``stored > SCHEMA_VERSION``
        # fail-closed guard. No-op today (both are 1), correct for future bumps.
        if stored < SCHEMA_VERSION:
            connection.execute(
                "UPDATE schema_meta SET value = ? WHERE key = 'version'",
                (str(SCHEMA_VERSION),),
            )
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()
            if row is None:
                raise StateStoreError("schema_meta is missing a version row")
            try:
                stored = int(row["value"])
            except ValueError as exc:
                raise StateStoreError("schema_meta version is not an integer") from exc
        connection.commit()
    except Exception:
        connection.close()
        raise
    # Restrict the database file to the owning user. The WAL/shm sidecar files
    # are protected by the 0o700 parent directory regardless of their own mode.
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass
    return connection


def load_job_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every persisted job row as a dict."""
    return [dict(row) for row in connection.execute("SELECT * FROM jobs").fetchall()]


def count_job_rows_for_project(
    connection: sqlite3.Connection, project_id: str
) -> int:
    """Return the number of persisted job rows for one project (M-GV-03).

    Used to identify the genesis job: the first job created for a project (zero
    prior rows) is the genesis job, which carries the apex-grounding burden.
    """
    row = connection.execute(
        "SELECT COUNT(*) AS count FROM jobs WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    return int(row["count"]) if row is not None else 0


def load_registered_apex_authority(
    connection: sqlite3.Connection, project_id: str
) -> dict[str, str] | None:
    """Return the registered apex-authority ref for a project, or None (M-GV-03).

    The registration ledger records the ``apex_authority_ref`` a project
    registered with. A loaded policy whose ref differs from this recorded value
    is a change without re-confirmation and must be rejected.
    """
    row = connection.execute(
        "SELECT apex_authority_ref, confirmed_at FROM registered_apex_authority "
        "WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "apex_authority_ref": row["apex_authority_ref"],
        "confirmed_at": row["confirmed_at"],
    }


def upsert_registered_apex_authority(
    connection: sqlite3.Connection,
    project_id: str,
    apex_authority_ref: str,
    confirmed_at: str | None = None,
) -> None:
    """Record or re-confirm a project's apex-authority ref (M-GV-03).

    Inserting a row for the first time is the registration moment; overwriting
    it is the explicit re-confirmation path (``ProjectRegistry.confirm_apex_authority``).
    """
    if confirmed_at is None:
        confirmed_at = datetime.now(UTC).isoformat()
    with connection:
        connection.execute(
            "INSERT INTO registered_apex_authority(project_id, apex_authority_ref, confirmed_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(project_id) DO UPDATE SET "
            "apex_authority_ref = excluded.apex_authority_ref, "
            "confirmed_at = excluded.confirmed_at",
            (project_id, apex_authority_ref, confirmed_at),
        )


def load_record_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every persisted verification row as a dict."""
    return [
        dict(row)
        for row in connection.execute("SELECT * FROM verifications").fetchall()
    ]


def load_last_passed_record(
    connection: sqlite3.Connection, project_id: str
) -> dict[str, Any] | None:
    """Return the most recent passed verification row for a project, or None.

    A record is 'passed' when every command in its ``commands_json`` reports
    status ``passed``. Used by M-CC-09 baseline comparison: the caller compares
    a candidate's fingerprint against this last green verification to report a
    deterministic repeat or a changed baseline.
    """
    rows = connection.execute(
        "SELECT * FROM verifications WHERE project_id = ? "
        "ORDER BY verified_at DESC",
        (project_id,),
    ).fetchall()
    for row in rows:
        try:
            commands = json.loads(row["commands_json"])
        except (json.JSONDecodeError, TypeError, KeyError):
            continue
        if isinstance(commands, list) and commands and all(
            isinstance(item, dict) and item.get("status") == "passed"
            for item in commands
        ):
            return dict(row)
    return None


def upsert_job_row(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Persist one job row as a single ACID transaction."""
    with connection:
        connection.execute(_UPSERT_JOB, row)


def upsert_record_row(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Persist one verification row as a single ACID transaction."""
    with connection:
        connection.execute(_UPSERT_RECORD, row)


_UPSERT_VERIFICATION_ENVIRONMENT = """
INSERT INTO verification_environment (
    verification_id, environment_digest
) VALUES (
    :verification_id, :environment_digest
)
ON CONFLICT(verification_id) DO UPDATE SET
    environment_digest = excluded.environment_digest
"""


def upsert_verification_environment_row(
    connection: sqlite3.Connection, row: dict[str, Any]
) -> None:
    """Persist one verification-environment sidecar row (M-EV-02).

    A 1:1 sidecar to ``verifications`` keyed by ``verification_id`` (FK with
    ``ON DELETE CASCADE`` so pruning a verification row prunes its environment
    digest too). Holds the ``environment_digest`` (SHA-256 over the Python
    interpreter version + the allowlisted deterministic env keys + the
    verification command argv + an optional toolchain-pin digest) so a
    verification result carries a stable, bounded environment/toolchain
    fingerprint. Additive: ``CREATE TABLE IF NOT EXISTS``, never alters
    ``verifications``, ``SCHEMA_VERSION`` stays 1.
    """
    with connection:
        connection.execute(_UPSERT_VERIFICATION_ENVIRONMENT, row)


def load_verification_environment_rows(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Return every persisted verification-environment sidecar row as a dict."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM verification_environment"
        ).fetchall()
    ]


def load_verification_environment_row(
    connection: sqlite3.Connection, verification_id: str
) -> dict[str, Any] | None:
    """Return one verification-environment sidecar row, or None."""
    row = connection.execute(
        "SELECT * FROM verification_environment WHERE verification_id = ?",
        (verification_id,),
    ).fetchone()
    return dict(row) if row is not None else None


_UPSERT_VERIFICATION_BINDING = """
INSERT INTO verification_bindings (
    verification_id, toolchain_digest, reviewer_independence_class,
    expires_at, authority_ref
) VALUES (
    :verification_id, :toolchain_digest, :reviewer_independence_class,
    :expires_at, :authority_ref
)
ON CONFLICT(verification_id) DO UPDATE SET
    toolchain_digest = excluded.toolchain_digest,
    reviewer_independence_class = excluded.reviewer_independence_class,
    expires_at = excluded.expires_at,
    authority_ref = excluded.authority_ref
"""


def upsert_verification_binding_row(
    connection: sqlite3.Connection, row: dict[str, Any]
) -> None:
    """Persist one verification-binding sidecar row (M-EV-03).

    A 1:1 sidecar to ``verifications`` keyed by ``verification_id`` (FK with
    ``ON DELETE CASCADE``). Holds the binding fields that complete the evidence
    set: ``toolchain_digest`` (the M-EV-02 environment/toolchain digest, copied
    here so the binding is self-contained), ``reviewer_independence_class``
    (NULL for verification bindings — only reviews carry one),
    ``expires_at`` (ISO-8601; NULL means no expiry), and ``authority_ref``
    (links to the ``authority_records`` from M-EV-05). Additive: ``CREATE TABLE
    IF NOT EXISTS``, never alters ``verifications``, ``SCHEMA_VERSION`` stays 1.
    """
    with connection:
        connection.execute(_UPSERT_VERIFICATION_BINDING, row)


def load_verification_binding_rows(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Return every persisted verification-binding sidecar row as a dict."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM verification_bindings"
        ).fetchall()
    ]


def load_verification_binding_row(
    connection: sqlite3.Connection, verification_id: str
) -> dict[str, Any] | None:
    """Return one verification-binding sidecar row, or None."""
    row = connection.execute(
        "SELECT * FROM verification_bindings WHERE verification_id = ?",
        (verification_id,),
    ).fetchone()
    return dict(row) if row is not None else None


_UPSERT_REVIEW_BINDING = """
INSERT INTO review_bindings (
    review_id, toolchain_digest, reviewer_independence_class,
    expires_at, authority_ref
) VALUES (
    :review_id, :toolchain_digest, :reviewer_independence_class,
    :expires_at, :authority_ref
)
ON CONFLICT(review_id) DO UPDATE SET
    toolchain_digest = excluded.toolchain_digest,
    reviewer_independence_class = excluded.reviewer_independence_class,
    expires_at = excluded.expires_at,
    authority_ref = excluded.authority_ref
"""


def upsert_review_binding_row(
    connection: sqlite3.Connection, row: dict[str, Any]
) -> None:
    """Persist one review-binding sidecar row (M-EV-03).

    A 1:1 sidecar to ``reviews`` keyed by ``review_id`` (FK with
    ``ON DELETE CASCADE``). Holds the binding fields that complete the review
    evidence set: ``toolchain_digest`` (optional environment/toolchain digest),
    ``reviewer_independence_class`` (one of ``bridge_spawned|human|external`` —
    stamped by ``review.py``: ``bridge_spawned`` for
    ``run_independent_review``, ``human``/``external`` for
    ``record_candidate_review``), ``expires_at`` (ISO-8601; NULL means no
    expiry), and ``authority_ref`` (links to the ``authority_records`` from
    M-EV-05). Additive: ``CREATE TABLE IF NOT EXISTS``, never alters ``reviews``,
    ``SCHEMA_VERSION`` stays 1.
    """
    with connection:
        connection.execute(_UPSERT_REVIEW_BINDING, row)


def load_review_binding_rows(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Return every persisted review-binding sidecar row as a dict."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM review_bindings"
        ).fetchall()
    ]


def load_review_binding_row(
    connection: sqlite3.Connection, review_id: str
) -> dict[str, Any] | None:
    """Return one review-binding sidecar row, or None."""
    row = connection.execute(
        "SELECT * FROM review_bindings WHERE review_id = ?",
        (review_id,),
    ).fetchone()
    return dict(row) if row is not None else None


# -- M-EV-04: review content-addressing -------------------------------------
# A 1:1 sidecar to ``reviews`` keyed by ``review_id`` (FK with
# ``ON DELETE CASCADE``) holding the ``verdict_hash`` — a 64-hex SHA-256 over a
# stable, deterministic canonicalization of the verdict text and the binding
# fields (``base_head``, ``diff_fingerprint``, the sorted ``paths`` list). The
# hash lets an orchestrator content-address a review without re-reading its
# verdict, and detect drift if the verdict or bindings change. Additive:
# ``CREATE TABLE IF NOT EXISTS``, never alters ``reviews``, ``SCHEMA_VERSION``
# stays 1.

_UPSERT_REVIEW_CONTENT = """
INSERT INTO review_content (
    review_id, verdict_hash
) VALUES (
    :review_id, :verdict_hash
)
ON CONFLICT(review_id) DO UPDATE SET
    verdict_hash = excluded.verdict_hash
"""


def upsert_review_content_row(
    connection: sqlite3.Connection, row: dict[str, Any]
) -> None:
    """Persist one review-content sidecar row as a single ACID transaction."""
    with connection:
        connection.execute(_UPSERT_REVIEW_CONTENT, row)


def load_review_content_rows(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Return every persisted review-content sidecar row as a dict."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM review_content"
        ).fetchall()
    ]


def load_review_content_row(
    connection: sqlite3.Connection, review_id: str
) -> dict[str, Any] | None:
    """Return one review-content sidecar row, or None."""
    row = connection.execute(
        "SELECT * FROM review_content WHERE review_id = ?",
        (review_id,),
    ).fetchone()
    return dict(row) if row is not None else None


# -- M-EV-04: revision -> finding links -------------------------------------
# An append-only audit ledger linking a revision job's checkpoint to the
# review findings it addresses. ``link_id`` is an autoincrement PK;
# ``review_id`` FK->reviews ``ON DELETE CASCADE``; ``resolving_job_id`` is the
# revision job; ``resolving_checkpoint_id`` FK->job_checkpoints (nullable so a
# revision recorded before the first checkpoint still links);
# ``finding_ref`` is an identifier of the finding within the review (an index,
# a hash of a finding snippet, or NULL when the review has a single verdict);
# ``resolved_at`` is an ISO-8601 timestamp. Additive: ``CREATE TABLE IF NOT
# EXISTS``, ``SCHEMA_VERSION`` stays 1.

_INSERT_REVISION_RESOLVES_FINDING = """
INSERT INTO revision_resolves_finding (
    review_id, resolving_job_id, resolving_checkpoint_id, finding_ref, resolved_at
) VALUES (
    :review_id, :resolving_job_id, :resolving_checkpoint_id, :finding_ref,
    :resolved_at
)
"""


def insert_revision_resolves_finding_row(
    connection: sqlite3.Connection, row: dict[str, Any]
) -> None:
    """Append one revision-resolves-finding row as a single ACID transaction."""
    with connection:
        connection.execute(_INSERT_REVISION_RESOLVES_FINDING, row)


def load_revision_resolves_finding_rows_for_review(
    connection: sqlite3.Connection, review_id: str
) -> list[dict[str, Any]]:
    """Return one review's revision-resolves-finding rows as dicts, oldest first.

    The queryable finding->revision->closure chain: each row links a finding
    reference in the review to the job/checkpoint that resolved it.
    """
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM revision_resolves_finding WHERE review_id = ? "
            "ORDER BY link_id ASC",
            (review_id,),
        ).fetchall()
    ]


def load_revision_resolves_finding_rows_for_job(
    connection: sqlite3.Connection, job_id: str
) -> list[dict[str, Any]]:
    """Return one job's revision-resolves-finding rows as dicts, oldest first."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM revision_resolves_finding WHERE resolving_job_id = ? "
            "ORDER BY link_id ASC",
            (job_id,),
        ).fetchall()
    ]


def job_id_exists(connection: sqlite3.Connection, job_id: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    return row is not None


def load_review_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every persisted review row as a dict."""
    return [
        dict(row) for row in connection.execute("SELECT * FROM reviews").fetchall()
    ]


def load_review_rows_for_job(
    connection: sqlite3.Connection, job_id: str
) -> list[dict[str, Any]]:
    """Return one job's persisted review rows as dicts."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM reviews WHERE job_id = ?", (job_id,)
        ).fetchall()
    ]


def load_record_rows_for_job(
    connection: sqlite3.Connection, job_id: str
) -> list[dict[str, Any]]:
    """Return one job's persisted verification rows as dicts."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM verifications WHERE job_id = ?", (job_id,)
        ).fetchall()
    ]


def upsert_review_row(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Persist one review row as a single ACID transaction."""
    with connection:
        connection.execute(_UPSERT_REVIEW, row)


def review_id_exists(connection: sqlite3.Connection, review_id: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM reviews WHERE review_id = ?", (review_id,)
    ).fetchone()
    return row is not None


def load_publication_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every persisted publication row as a dict."""
    return [
        dict(row)
        for row in connection.execute("SELECT * FROM publications").fetchall()
    ]


def upsert_publication_row(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Persist one publication row as a single ACID transaction."""
    with connection:
        connection.execute(_UPSERT_PUBLICATION, row)


def publication_for(
    connection: sqlite3.Connection,
    project_id: str,
    job_id: str,
) -> dict[str, Any] | None:
    """Return the publication row for one (project, job), or None."""
    row = connection.execute(
        "SELECT * FROM publications WHERE project_id = ? AND job_id = ?",
        (project_id, job_id),
    ).fetchone()
    return dict(row) if row is not None else None


def used_verification_record(
    connection: sqlite3.Connection,
    project_id: str,
    job_id: str,
    base_head: str,
    diff_fingerprint: str,
) -> dict[str, Any] | None:
    """Return the used verification record matching the exact evidence, or None.

    A publication is authorized by a verification record that the bridge
    consumed during ``commit_verified_development_job`` (``used = 1``) and whose
    ``(base_head, diff_fingerprint)`` match the evidence the caller asserts.
    """
    row = connection.execute(
        "SELECT * FROM verifications "
        "WHERE project_id = ? AND job_id = ? AND base_head = ? "
        "AND diff_fingerprint = ? AND used = 1",
        (project_id, job_id, base_head, diff_fingerprint),
    ).fetchone()
    return dict(row) if row is not None else None


def load_sync_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every persisted sync-audit row as a dict, oldest first."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM syncs ORDER BY sync_id ASC"
        ).fetchall()
    ]


def insert_sync_row(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Append one sync-audit row as a single ACID transaction.

    Syncs are an append-only audit log: each successful synchronization adds a
    row, keyed by an autoincrement ``sync_id`` rather than a natural key, so a
    project may be synchronized many times over its lifetime. Unlike jobs or
    publications, no single-use bound is enforced from this table; it exists
    for restart-safe auditability, mirroring the durable-state philosophy.
    """
    with connection:
        connection.execute(_INSERT_SYNC, row)


def insert_checkpoint_row(connection: sqlite3.Connection, row: dict[str, Any]) -> int:
    """Append one checkpoint row as a single ACID transaction.

    Checkpoints are an append-only, turn-level mission-state ledger: each
    sample/turn/boundary/extension/milestone adds a row, keyed by an
    autoincrement ``checkpoint_id`` and ordered per job by the monotonic
    ``seq``. A crashed job's last checkpoint is what makes it resumable rather
    than discarded. No single-use bound; the table exists for restart-safe
    resumability and progress intelligence.

    Returns the autoincrement ``checkpoint_id`` of the inserted row so the
    caller can attach a sidecar detail row (M-CT-03).
    """
    with connection:
        cursor = connection.execute(_INSERT_CHECKPOINT, row)
        return int(cursor.lastrowid)


def load_checkpoint_rows(
    connection: sqlite3.Connection, job_id: str
) -> list[dict[str, Any]]:
    """Return one job's checkpoint rows as dicts, ordered by ``seq`` then id."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM job_checkpoints WHERE job_id = ? "
            "ORDER BY seq ASC, checkpoint_id ASC",
            (job_id,),
        ).fetchall()
    ]


def load_latest_checkpoint(
    connection: sqlite3.Connection, job_id: str
) -> dict[str, Any] | None:
    """Return the most recent checkpoint row for one job, or None."""
    row = connection.execute(
        "SELECT * FROM job_checkpoints WHERE job_id = ? "
        "ORDER BY seq DESC, checkpoint_id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def checkpoint_count(connection: sqlite3.Connection, job_id: str) -> int:
    """Return the number of checkpoint rows for one job."""
    row = connection.execute(
        "SELECT COUNT(*) FROM job_checkpoints WHERE job_id = ?", (job_id,)
    ).fetchone()
    return int(row[0]) if row is not None else 0


def upsert_job_worktree_meta(
    connection: sqlite3.Connection, row: dict[str, Any]
) -> None:
    """Persist one job's worktree base commit as a single ACID transaction.

    M-CC-10: a 1:1 sidecar to ``jobs`` keyed by ``job_id`` that durably
    records the worktree's ``base_head`` (and, when known, its path) so a lost
    worktree can be reconstructed from the content-addressed checkpoint ledger
    even after a server restart — the ``jobs`` row itself nulls
    ``worktree_base_head`` when the directory vanishes. The upsert is
    idempotent and additive; ``SCHEMA_VERSION`` stays 1.
    """
    with connection:
        connection.execute(_UPSERT_JOB_WORKTREE_META, row)


def load_job_worktree_meta(
    connection: sqlite3.Connection, job_id: str
) -> dict[str, Any] | None:
    """Return one job's worktree meta row, or None when none is recorded."""
    row = connection.execute(
        "SELECT * FROM job_worktree_meta WHERE job_id = ?", (job_id,)
    ).fetchone()
    return dict(row) if row is not None else None


_INSERT_CHECKPOINT_DETAIL = """
INSERT INTO job_checkpoint_detail (
    checkpoint_id, completed_steps_json, remaining_steps_json,
    tests_json, decisions_json, tool_history_json
) VALUES (
    :checkpoint_id, :completed_steps_json, :remaining_steps_json,
    :tests_json, :decisions_json, :tool_history_json
)
ON CONFLICT(checkpoint_id) DO UPDATE SET
    completed_steps_json = excluded.completed_steps_json,
    remaining_steps_json = excluded.remaining_steps_json,
    tests_json = excluded.tests_json,
    decisions_json = excluded.decisions_json,
    tool_history_json = excluded.tool_history_json
"""


def insert_checkpoint_detail_row(
    connection: sqlite3.Connection, row: dict[str, Any]
) -> None:
    """Persist one checkpoint detail sidecar row as a single ACID transaction.

    M-CT-03: a 1:1 sidecar to ``job_checkpoints`` keyed by ``checkpoint_id``
    (INTEGER PRIMARY KEY, FK to ``job_checkpoints.checkpoint_id`` with
    ``ON DELETE CASCADE`` so compaction prunes the detail too). All five rich
    fields (completed/remaining steps, tests, decisions, tool history) are
    NULLABLE TEXT JSON — a checkpoint with no structured events simply has
    NULL fields. The upsert makes re-recording a detail row idempotent.
    ``SCHEMA_VERSION`` stays 1; the table is additive (``CREATE TABLE IF NOT
    EXISTS``) and never alters ``job_checkpoints``.
    """
    with connection:
        connection.execute(_INSERT_CHECKPOINT_DETAIL, row)


def load_checkpoint_detail_rows(
    connection: sqlite3.Connection,
    checkpoint_ids: list[int],
) -> list[dict[str, Any]]:
    """Return detail sidecar rows for the given checkpoint ids (M-CT-03).

    Ordered by ``checkpoint_id`` ascending. Returns an empty list when no ids
    are given or no rows match. Used by the snapshot/resume path to rehydrate
    the rich fields for a specific checkpoint.
    """
    if not checkpoint_ids:
        return []
    placeholders = ",".join("?" for _ in checkpoint_ids)
    return [
        dict(row)
        for row in connection.execute(
            f"SELECT * FROM job_checkpoint_detail "
            f"WHERE checkpoint_id IN ({placeholders}) "
            f"ORDER BY checkpoint_id ASC",
            tuple(checkpoint_ids),
        ).fetchall()
    ]


def load_checkpoint_detail_for_job(
    connection: sqlite3.Connection,
    job_id: str,
) -> list[dict[str, Any]]:
    """Return all detail sidecar rows for one job, ordered by checkpoint seq.

    M-CT-03: joins ``job_checkpoint_detail`` to ``job_checkpoints`` on
    ``checkpoint_id`` and filters by ``job_id``, so a reload or replay can
    walk the full rich-field history of a job's checkpoints.
    """
    return [
        dict(row)
        for row in connection.execute(
            "SELECT d.* FROM job_checkpoint_detail d "
            "JOIN job_checkpoints c ON d.checkpoint_id = c.checkpoint_id "
            "WHERE c.job_id = ? ORDER BY c.seq ASC, c.checkpoint_id ASC",
            (job_id,),
        ).fetchall()
    ]


def compact_checkpoints(
    connection: sqlite3.Connection,
    job_id: str,
    keep_kinds: tuple[str, ...] = ("boundary", "extension", "milestone"),
    keep_latest_n: int = 50,
) -> int:
    """Prune per-job checkpoint rows, keeping structural rows and the recent tail.

    All rows whose ``kind`` is in ``keep_kinds`` are retained (they mark
    boundaries, extensions, and milestone closures — small in number and
    structurally important). Of the remaining ``turn``/``sample``/``tool`` rows,
    only the most recent ``keep_latest_n`` (by ``seq``) are kept; the rest are
    deleted. Returns the number of rows deleted. Promoted evidence
    (verifications/reviews/publications) is never touched — only checkpoints.
    """
    placeholders = ",".join("?" for _ in keep_kinds)
    with connection:
        cursor = connection.execute(
            f"DELETE FROM job_checkpoints WHERE job_id = ? "
            f"AND kind NOT IN ({placeholders}) "
            f"AND seq NOT IN ("
            f"  SELECT seq FROM job_checkpoints WHERE job_id = ? "
            f"  AND kind NOT IN ({placeholders}) "
            f"  ORDER BY seq DESC LIMIT ?"
            f")",
            (
                job_id,
                *keep_kinds,
                job_id,
                *keep_kinds,
                keep_latest_n,
            ),
        )
        return int(cursor.rowcount or 0)


# M-EV-08: structural checkpoint kinds that GC always retains, mirroring the
# ``compact_checkpoints`` default ``keep_kinds``. Boundary/extension/milestone
# rows are small in number and structurally important (they mark slice
# boundaries, granted extensions, and milestone closures), so time-based GC
# never reclaims them — only ``sample``/``turn``/``tool`` rows are eligible.
_GC_KEEP_KINDS: tuple[str, ...] = ("boundary", "extension", "milestone")


def gc_artifacts(
    connection: sqlite3.Connection,
    evidence_retention_days: int,
    checkpoint_retention_days: int,
    *,
    now_iso: str | None = None,
) -> int:
    """Time-based artifact GC for the durable state database.

    Deletes only non-promoted checkpoint (+ cascaded ``job_checkpoint_detail``)
    rows whose ``recorded_at`` is older than ``now - checkpoint_retention_days``.
    Returns the number of deleted checkpoint rows.

    Invariant (promoted-evidence-safe):

      Promoted evidence (verifications, reviews, publications, syncs, and the
      checkpoints they reference) is never deleted by GC; only non-promoted,
      non-structural checkpoint/detail rows older than the retention window are
      reclaimed.

    A checkpoint is protected (never deleted) if ANY of:

      * its ``job_id`` has a row in ``verifications`` with ``used = 1`` (a
        committed verification), OR
      * its ``job_id`` has a row in ``reviews`` or ``publications``, OR
      * it is directly referenced by
        ``revision_resolves_finding.resolving_checkpoint_id``.

    (The ``syncs`` table is project-level with no ``job_id`` column, so it
    cannot protect a specific job's checkpoints by id; it is still never
    deleted by GC — only ``job_checkpoints`` are ever touched here.)

    In addition, structural kinds (``boundary``/``extension``/``milestone``)
    are always retained, mirroring ``compact_checkpoints``. Rows from
    ``verifications``, ``reviews``, ``publications``, ``syncs``,
    ``authorizations``, ``requirements``/``requirement_links``,
    ``verification_environment``, ``verification_bindings``,
    ``review_bindings``, ``review_content``, and ``source_classifications`` are
    never touched.

    ``evidence_retention_days`` is accepted as policy config but does not
    drive any delete here — promoted evidence is never deleted regardless of
    its value (it is recorded for future policy use and to keep the GC
    interface self-documenting). ``checkpoint_retention_days == 0`` (the
    default) disables time-based GC: the function is a no-op and returns 0.

    Deterministic + testable: store never reads the wall clock. The caller may
    pin the reference time via ``now_iso`` (an ISO-8601 string in the same
    ``recorded_at`` shape, e.g. ``2026-08-26T00:00:00Z``); when ``None``, the
    cutoff is computed from the current UTC time at the call site. The cutoff
    timestamp is compared lexicographically against ``recorded_at`` (both are
    fixed-width ``YYYY-MM-DDTHH:MM:SSZ`` strings, so string comparison is
    chronological). ``SCHEMA_VERSION`` stays 1; the supporting index is
    additive (``CREATE INDEX IF NOT EXISTS``).
    """
    if checkpoint_retention_days <= 0:
        # Time-based GC disabled (the default). evidence_retention_days is
        # policy-only and never deletes evidence; with checkpoint GC disabled
        # there is nothing to do.
        return 0

    # Compute the cutoff timestamp (oldest a checkpoint may be to survive).
    # ``recorded_at`` is stored as ``YYYY-MM-DDTHH:MM:SSZ``; we build the
    # cutoff in the same shape so a lexicographic compare is chronological.
    from datetime import UTC, datetime, timedelta

    if now_iso is None:
        now_dt = datetime.now(UTC).replace(microsecond=0)
    else:
        # Parse the caller-supplied reference time. Accept either a trailing
        # ``Z`` or a numeric offset; normalize to UTC.
        now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=UTC)
        else:
            now_dt = now_dt.astimezone(UTC)
    cutoff_dt = now_dt - timedelta(days=checkpoint_retention_days)
    cutoff = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    keep_kind_placeholders = ",".join("?" for _ in _GC_KEEP_KINDS)

    # Protected set 1: job_ids that have promoted evidence. A job with a
    # committed verification (used=1), OR any review/publication row, is
    # promoted — all of its checkpoints survive GC forever. (The ``syncs``
    # table is project-level with no ``job_id`` column, so it cannot protect
    # a specific job's checkpoints; it is still never deleted by GC — only
    # ``job_checkpoints`` are ever touched here.)
    protected_job_ids: set[str] = set()
    for row in connection.execute(
        "SELECT DISTINCT job_id FROM verifications WHERE used = 1"
    ).fetchall():
        protected_job_ids.add(row["job_id"])
    for statement in (
        "SELECT DISTINCT job_id FROM reviews",
        "SELECT DISTINCT job_id FROM publications",
    ):
        for row in connection.execute(statement).fetchall():
            protected_job_ids.add(row["job_id"])

    # Protected set 2: checkpoint_ids directly referenced by a
    # revision→finding resolution link. These survive GC even if the job has
    # no promoted evidence row.
    protected_checkpoint_ids: set[int] = set()
    for row in connection.execute(
        "SELECT DISTINCT resolving_checkpoint_id FROM revision_resolves_finding "
        "WHERE resolving_checkpoint_id IS NOT NULL"
    ).fetchall():
        protected_checkpoint_ids.add(int(row["resolving_checkpoint_id"]))

    # Build the DELETE with parameterized NOT-IN guards. Both protected sets
    # are baked into the query as literals only after failing-closed checks
    # (the values are internal IDs, never caller-supplied free text). Use
    # parameter placeholders for the cutoff and the keep-kinds tuple.
    with connection:
        if protected_job_ids:
            job_placeholders = ",".join("?" for _ in protected_job_ids)
            job_guard = f"AND job_id NOT IN ({job_placeholders}) "
            job_params: tuple[Any, ...] = tuple(protected_job_ids)
        else:
            job_guard = ""
            job_params = ()
        if protected_checkpoint_ids:
            cp_placeholders = ",".join("?" for _ in protected_checkpoint_ids)
            cp_guard = f"AND checkpoint_id NOT IN ({cp_placeholders}) "
            cp_params: tuple[Any, ...] = tuple(protected_checkpoint_ids)
        else:
            cp_guard = ""
            cp_params = ()
        cursor = connection.execute(
            f"DELETE FROM job_checkpoints "
            f"WHERE recorded_at < ? "
            f"AND kind NOT IN ({keep_kind_placeholders}) "
            f"{job_guard}{cp_guard}",
            (
                cutoff,
                *_GC_KEEP_KINDS,
                *job_params,
                *cp_params,
            ),
        )
        deleted = int(cursor.rowcount or 0)
    # The job_checkpoint_detail rows cascade via the FK ON DELETE CASCADE
    # (foreign_keys=ON in open_state_db). Callers verify the cascade in tests.
    return deleted


def _seed_source_classifications(connection: sqlite3.Connection) -> None:
    """Seed the fixed source-classification vocabulary (idempotent).

    M-EV-05: every authority record carries one of these classifications so an
    orchestrator can reason about how authoritative a governance instruction
    is. The seed is additive and never overwrites an existing row.
    """
    classifications = (
        ("canonical", "the authoritative, current source of truth"),
        ("historical", "a past state, no longer current"),
        ("asserted", "claimed by an actor without independent verification"),
        ("observed", "directly observed from the system or repository"),
        ("tested", "confirmed by a deterministic verification run"),
        ("inferred", "derived by reasoning from other evidence"),
        ("superseded", "replaced by a later, higher-authority instruction"),
    )
    with connection:
        connection.executemany(
            "INSERT OR IGNORE INTO source_classifications "
            "(classification, description) VALUES (?, ?)",
            classifications,
        )


def insert_event_row(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Append one audit-event row as a single ACID transaction.

    The event ledger is append-only (autoincrement ``event_id``): every job
    state transition, checkpoint, extension grant, pause, and terminal outcome
    appends a row. It is the durable audit source of truth, distinct from the
    in-place ``jobs`` row.
    """
    with connection:
        connection.execute(_INSERT_EVENT, row)


def load_event_rows(
    connection: sqlite3.Connection, job_id: str
) -> list[dict[str, Any]]:
    """Return one job's audit-event rows as dicts, oldest first."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM job_events WHERE job_id = ? ORDER BY event_id ASC",
            (job_id,),
        ).fetchall()
    ]


_INSERT_AUTHORITY = """
INSERT INTO authority_records (
    authority_id, job_id, event_id, actor, authority_source, scope,
    classification, recorded_at, detail_json
) VALUES (
    :authority_id, :job_id, :event_id, :actor, :authority_source, :scope,
    :classification, :recorded_at, :detail_json
)
"""


def insert_authority_record(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Append one authority record as a single ACID transaction.

    M-EV-05: a sidecar to the append-only ``job_events`` ledger. Each
    governance-changing event (status / extension / pause / terminal) is
    stamped with an ``authority_id`` in its ``detail_json``, and the full
    authority record (actor, authority source, scope, classification,
    timestamp) is persisted here so the authority behind every instruction is
    durable and queryable.
    """
    with connection:
        connection.execute(_INSERT_AUTHORITY, row)


def load_authority_records(
    connection: sqlite3.Connection, job_id: str
) -> list[dict[str, Any]]:
    """Return one job's authority records as dicts, oldest first."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM authority_records WHERE job_id = ? "
            "ORDER BY recorded_at ASC, authority_id ASC",
            (job_id,),
        ).fetchall()
    ]


def load_source_classifications(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Return the seeded source-classification vocabulary."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT classification, description FROM source_classifications "
            "ORDER BY classification ASC"
        ).fetchall()
    ]


_INSERT_SUPPLY_CHAIN_SCAN = """
INSERT INTO supply_chain_scans (
    project_id, job_id, verification_id, scan_command, scan_argv,
    exit_code, passed, scanned_at, detail_json
) VALUES (
    :project_id, :job_id, :verification_id, :scan_command, :scan_argv,
    :exit_code, :passed, :scanned_at, :detail_json
)
"""


def insert_supply_chain_scan(
    connection: sqlite3.Connection, row: dict[str, Any]
) -> None:
    """Append one supply-chain scan result as a single ACID transaction.

    M-CC-11: an additive audit row for each severity-gated supply-chain scan
    run at commit time. Records the scanner, its fixed argv, the exit code, and
    whether the gate passed, so a rejected commit leaves a durable audit trail
    for an orchestrator or operator.
    """
    with connection:
        connection.execute(_INSERT_SUPPLY_CHAIN_SCAN, row)


def load_supply_chain_scans(
    connection: sqlite3.Connection, job_id: str
) -> list[dict[str, Any]]:
    """Return one job's supply-chain scan rows as dicts, oldest first."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM supply_chain_scans WHERE job_id = ? "
            "ORDER BY scan_id ASC",
            (job_id,),
        ).fetchall()
    ]


_INSERT_EVIDENCE_MANIFEST = """
INSERT INTO evidence_manifests (
    project_id, job_id, bundle_sha256, algorithm, signature, signed_at, detail_json
) VALUES (
    :project_id, :job_id, :bundle_sha256, :algorithm, :signature, :signed_at,
    :detail_json
)
"""


def insert_evidence_manifest(
    connection: sqlite3.Connection, row: dict[str, Any]
) -> None:
    """Persist one signed evidence manifest as a single ACID transaction.

    M-CC-11: the signature of the evidence bundle is stored alongside the
    bundle (durably, in the private state DB) so an orchestrator or operator
    can audit that a bundle was signed with the project key. Additive table;
    ``SCHEMA_VERSION`` stays 1.
    """
    with connection:
        connection.execute(_INSERT_EVIDENCE_MANIFEST, row)


def load_evidence_manifests(
    connection: sqlite3.Connection, job_id: str
) -> list[dict[str, Any]]:
    """Return one job's signed evidence manifests as dicts, oldest first."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM evidence_manifests WHERE job_id = ? "
            "ORDER BY manifest_id ASC",
            (job_id,),
        ).fetchall()
    ]


def upsert_progress_row(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Persist one job's progress/budget sidecar row as a single ACID transaction.

    ``job_progress`` is a sidecar to ``jobs`` keyed by ``job_id``: the
    turn-level budget counters (turns/tokens/extensions/no-progress), the
    pause reason, and the milestone plan. Keeping it separate from the
    ``jobs`` row makes the schema change purely additive (no column added to
    ``jobs``, so existing rows and tests are untouched) while still
    SCHEMA_VERSION 1. Re-hydrated synchronously in ``DevelopmentJobManager``.
    """
    with connection:
        # Backwards-compatible: callers that build a progress row without the
        # additive M-GV-05 drafter_id key still bind it (as NULL) instead of
        # tripping sqlite's "did not supply a value for binding parameter".
        connection.execute(_UPSERT_PROGRESS, {**row, "drafter_id": row.get("drafter_id")})


def load_progress_row(
    connection: sqlite3.Connection, job_id: str
) -> dict[str, Any] | None:
    """Return one job's progress/budget row, or None if it has none yet."""
    row = connection.execute(
        "SELECT * FROM job_progress WHERE job_id = ?", (job_id,)
    ).fetchone()
    return dict(row) if row is not None else None


_INSERT_DELEGATION = """
INSERT INTO delegations (
    parent_job_id, child_job_id, project_id, mandate_json, created_at
) VALUES (
    :parent_job_id, :child_job_id, :project_id, :mandate_json, :created_at
)
"""


_INSERT_RATIFICATION = """
INSERT INTO ratifications (
    ratification_id, project_id, job_id, candidate_fingerprint, base_head,
    actor, authority_source, ratified_at, detail_json
) VALUES (
    :ratification_id, :project_id, :job_id, :candidate_fingerprint, :base_head,
    :actor, :authority_source, :ratified_at, :detail_json
)
"""


def insert_delegation_row(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Append one parent->child delegation record as a single ACID transaction.

    Delegations are an append-only audit ledger: each bounded sub-agent
    delegation adds a row linking the parent job to its child job, with the
    mandate (inputs/outputs/authority) that bounds the child's scope. No
    single-use bound; the table exists for restart-safe tree reconstruction.
    """
    with connection:
        connection.execute(_INSERT_DELEGATION, row)


def load_delegation_rows_for_parent(
    connection: sqlite3.Connection, parent_job_id: str
) -> list[dict[str, Any]]:
    """Return one parent job's delegation rows as dicts, oldest first."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM delegations WHERE parent_job_id = ? "
            "ORDER BY delegation_id ASC",
            (parent_job_id,),
        ).fetchall()
    ]


def insert_ratification_row(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Append one governance ratification record as a single ACID transaction.

    Ratifications (M-CC-04, Option A) are an append-only governance ledger,
    deliberately distinct from any merge/tag/release/deploy action (those remain
    in ``PROHIBITED_ACTIONS``). Each row records that a named actor, drawing on
    a stated authority source, ratified the completed candidate for a job at its
    pinned ``base_head``. ``candidate_fingerprint`` binds the ratification to the
    candidate's working-tree evidence (e.g. the latest checkpoint's
    ``partial_fingerprint``). SCHEMA_VERSION stays 1: the table is additive
    (``CREATE TABLE IF NOT EXISTS``).
    """
    with connection:
        connection.execute(_INSERT_RATIFICATION, row)


def load_ratification_rows(
    connection: sqlite3.Connection, job_id: str
) -> list[dict[str, Any]]:
    """Return one job's ratification rows as dicts, oldest first."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM ratifications WHERE job_id = ? "
            "ORDER BY ratified_at ASC, ratification_id ASC",
            (job_id,),
        ).fetchall()
    ]


def upsert_requirement_row(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Persist one requirement row as a single ACID transaction."""
    with connection:
        connection.execute(_UPSERT_REQUIREMENT, row)


def upsert_requirement_link_row(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Persist one requirement link row as a single ACID transaction."""
    with connection:
        connection.execute(_UPSERT_REQUIREMENT_LINK, row)


def load_requirement_links_for_job(connection: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    """Return all requirement links for a given job."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM requirement_links WHERE job_id = ?", (job_id,)
        ).fetchall()
    ]


def load_requirement_links_for_requirement(connection: sqlite3.Connection, requirement_id: str) -> list[dict[str, Any]]:
    """Return all requirement links for a given requirement."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM requirement_links WHERE requirement_id = ?", (requirement_id,)
        ).fetchall()
    ]


def load_requirements_for_project(
    connection: sqlite3.Connection, project_id: str
) -> list[dict[str, Any]]:
    """Return all requirement rows for a given project (M-EV-01)."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM requirements WHERE project_id = ? "
            "ORDER BY source_path ASC, source_line ASC",
            (project_id,),
        ).fetchall()
    ]


def update_requirement_status(
    connection: sqlite3.Connection,
    requirement_id: str,
    status: str,
    deferred_authority: str | None = None,
) -> None:
    """Update a requirement's status (and optionally deferred_authority) in place.

    M-EV-01: used to record a deferral (status='deferred' + authority) or to
    mark a requirement implemented/unimplemented. Additive: only touches the
    new status/deferred_authority columns.
    """
    with connection:
        if deferred_authority is not None:
            connection.execute(
                "UPDATE requirements SET status = ?, deferred_authority = ?, "
                "updated_at = ? WHERE requirement_id = ?",
                (status, deferred_authority, _timestamp_iso(), requirement_id),
            )
        else:
            connection.execute(
                "UPDATE requirements SET status = ?, updated_at = ? "
                "WHERE requirement_id = ?",
                (status, _timestamp_iso(), requirement_id),
            )


def _timestamp_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


_INSERT_AUTHORIZATION = """
INSERT INTO authorizations (
    authorization_id, project_id, job_id, actor, authority_source,
    capability, target, issued_at, expires_at, single_use, used,
    delegated_from, detail_json
) VALUES (
    :authorization_id, :project_id, :job_id, :actor, :authority_source,
    :capability, :target, :issued_at, :expires_at, :single_use, :used,
    :delegated_from, :detail_json
)
"""


def insert_authorization_row(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Persist one authorization row as a single ACID transaction."""
    with connection:
        connection.execute(_INSERT_AUTHORIZATION, row)


def load_authorization_row(
    connection: sqlite3.Connection, authorization_id: str
) -> dict[str, Any] | None:
    """Return one authorization row, or None if it does not exist."""
    row = connection.execute(
        "SELECT * FROM authorizations WHERE authorization_id = ?",
        (authorization_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def load_authorization_rows_for(
    connection: sqlite3.Connection,
    project_id: str,
) -> list[dict[str, Any]]:
    """Return a project's unused authorization rows as dicts, oldest first.

    M-CC-08: the dashboard enrichment surfaces the project's in-force
    (unused) authorizations. Expiry filtering is left to the caller
    (``AuthorizationManager.list_active_for``) so this store reader stays a
    plain, dependency-free projection.
    """
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM authorizations WHERE project_id = ? AND used = 0 "
            "ORDER BY issued_at ASC, authorization_id ASC",
            (project_id,),
        ).fetchall()
    ]


def mark_authorization_used(
    connection: sqlite3.Connection, authorization_id: str
) -> bool:
    """Atomically mark one authorization as used (single-use consumption).

    The ``WHERE used = 0`` guard makes consumption atomic: only the first
    concurrent consumer wins. Returns True if this call performed the
    consumption, False if the authorization was already used (so the caller can
    raise the appropriate error).
    """
    with connection:
        cursor = connection.execute(
            "UPDATE authorizations SET used = 1 "
            "WHERE authorization_id = ? AND used = 0",
            (authorization_id,),
        )
        return cursor.rowcount > 0


_INSERT_RUNTIME_VERIFICATION = """
INSERT INTO runtime_verifications (
    runtime_verification_id, project_id, check_id, expected_head, verified_head,
    status, exit_code, started_at, completed_at, stdout_json, stderr_json,
    stdout_hash, stderr_hash, policy_fingerprint, evidence_fingerprint,
    repository_json, verified_at
) VALUES (
    :runtime_verification_id, :project_id, :check_id, :expected_head,
    :verified_head, :status, :exit_code, :started_at, :completed_at,
    :stdout_json, :stderr_json, :stdout_hash, :stderr_hash, :policy_fingerprint,
    :evidence_fingerprint, :repository_json, :verified_at
)
"""


def insert_runtime_verification_row(
    connection: sqlite3.Connection, row: dict[str, Any]
) -> None:
    """Append one runtime-verification evidence row as a single ACID transaction.

    MCPB-011: an append-only audit ledger keyed by ``runtime_verification_id``
    (the ``rtv_``-prefixed namespace, never the candidate ``verification_id``
    namespace). Both PASS and bounded FAIL records are persisted so an
    orchestrator or operator can audit every runtime check; only bounded,
    redacted data is stored. No single-use bound: a check may run many times
    over a project's lifetime, mirroring the append-only ``syncs`` audit table.
    """
    with connection:
        connection.execute(_INSERT_RUNTIME_VERIFICATION, row)


def load_runtime_verification_rows(
    connection: sqlite3.Connection,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return persisted runtime-verification rows as dicts, oldest first.

    Optionally filtered by ``project_id``. Read-only accessor for inspection;
    the ``runtime_verifications`` table is append-only.
    """
    if project_id is None:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM runtime_verifications ORDER BY verified_at ASC, "
                "runtime_verification_id ASC"
            ).fetchall()
        ]
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM runtime_verifications WHERE project_id = ? "
            "ORDER BY verified_at ASC, runtime_verification_id ASC",
            (project_id,),
        ).fetchall()
    ]


_INSERT_WORKING_TREE_CAPTURE = """
INSERT INTO working_tree_captures (
    capture_id, project_id, job_id, base_head, working_tree_fingerprint,
    candidate_fingerprint, include_paths_json, protected_exclude_paths_json,
    manifest_json, protected_exclude_manifest_json, status, error, captured_at
) VALUES (
    :capture_id, :project_id, :job_id, :base_head, :working_tree_fingerprint,
    :candidate_fingerprint, :include_paths_json, :protected_exclude_paths_json,
    :manifest_json, :protected_exclude_manifest_json, :status, :error,
    :captured_at
)
"""


def insert_working_tree_capture_row(
    connection: sqlite3.Connection, row: dict[str, Any]
) -> None:
    """Append one working-tree capture evidence row as a single ACID transaction.

    MCPB-012: an append-only audit ledger keyed by ``capture_id`` (equal to the
    retained job's ``job_id`` for successful captures; a random id with an empty
    ``job_id`` for rejected attempts). Both successful and rejected captures are
    persisted so an orchestrator or operator can audit every capture attempt and
    its race/provenance outcome; only successful captures bind a usable job.
    Mirrors the append-only ``runtime_verifications`` audit table.
    """
    with connection:
        connection.execute(_INSERT_WORKING_TREE_CAPTURE, row)


def load_working_tree_capture_rows(
    connection: sqlite3.Connection,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return persisted working-tree capture rows as dicts, oldest first.

    Optionally filtered by ``project_id``. Read-only accessor for inspection;
    the ``working_tree_captures`` table is append-only.
    """
    if project_id is None:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM working_tree_captures "
                "ORDER BY captured_at ASC, capture_id ASC"
            ).fetchall()
        ]
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM working_tree_captures WHERE project_id = ? "
            "ORDER BY captured_at ASC, capture_id ASC",
            (project_id,),
        ).fetchall()
    ]
def insert_oh_ledger_row(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Append one OH-001D ledger record as a single ACID transaction."""
    with connection:
        connection.execute(
            """
            INSERT INTO oh_ledger (
                ledger_id, operation_id, canonical_baseline_fingerprint,
                disposable_candidate_fingerprint, session_id, permitted_tools,
                observed_tool_usage, command_results_json, inspection_result,
                canonical_verification_result, teardown_result,
                residue_verification_result, evidence_bundle_id,
                evidence_fingerprint, final_verdict, failure_reason, recorded_at
            ) VALUES (
                :ledger_id, :operation_id, :canonical_baseline_fingerprint,
                :disposable_candidate_fingerprint, :session_id, :permitted_tools,
                :observed_tool_usage, :command_results_json, :inspection_result,
                :canonical_verification_result, :teardown_result,
                :residue_verification_result, :evidence_bundle_id,
                :evidence_fingerprint, :final_verdict, :failure_reason, :recorded_at
            )
            """,
            row,
        )

def load_oh_ledger_row(connection: sqlite3.Connection, ledger_id: str) -> dict[str, Any] | None:
    """Return one OH-001D ledger record by ID, or None."""
    row = connection.execute(
        "SELECT * FROM oh_ledger WHERE ledger_id = ?", (ledger_id,)
    ).fetchone()
    return dict(row) if row is not None else None
