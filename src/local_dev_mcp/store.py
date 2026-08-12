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

_UPSERT_PROGRESS = """
INSERT INTO job_progress (
    job_id, pause_reason, turns_used, tokens_used, extensions_granted,
    no_progress_turns, tool_failures, soft_warned, milestones_json,
    parent_job_id, delegation_mandate_json, updated_at
) VALUES (
    :job_id, :pause_reason, :turns_used, :tokens_used, :extensions_granted,
    :no_progress_turns, :tool_failures, :soft_warned, :milestones_json,
    :parent_job_id, :delegation_mandate_json, :updated_at
)
ON CONFLICT(job_id) DO UPDATE SET
    pause_reason = excluded.pause_reason,
    turns_used = excluded.turns_used,
    tokens_used = excluded.tokens_used,
    extensions_granted = excluded.extensions_granted,
    no_progress_turns = excluded.no_progress_turns,
    tool_failures = excluded.tool_failures,
    soft_warned = excluded.soft_warned,
    milestones_json = excluded.milestones_json,
    parent_job_id = excluded.parent_job_id,
    delegation_mandate_json = excluded.delegation_mandate_json,
    updated_at = excluded.updated_at
"""

_UPSERT_REQUIREMENT = """
INSERT INTO requirements (
    requirement_id, project_id, text, source_path, source_line, normative
) VALUES (
    :requirement_id, :project_id, :text, :source_path, :source_line, :normative
)
ON CONFLICT(requirement_id) DO UPDATE SET
    project_id = excluded.project_id,
    text = excluded.text,
    source_path = excluded.source_path,
    source_line = excluded.source_line,
    normative = excluded.normative
"""

_UPSERT_REQUIREMENT_LINK = """
INSERT INTO requirement_links (
    requirement_id, job_id, link_type, link_value
) VALUES (
    :requirement_id, :job_id, :link_type, :link_value
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


def insert_checkpoint_row(connection: sqlite3.Connection, row: dict[str, Any]) -> None:
    """Append one checkpoint row as a single ACID transaction.

    Checkpoints are an append-only, turn-level mission-state ledger: each
    sample/turn/boundary/extension/milestone adds a row, keyed by an
    autoincrement ``checkpoint_id`` and ordered per job by the monotonic
    ``seq``. A crashed job's last checkpoint is what makes it resumable rather
    than discarded. No single-use bound; the table exists for restart-safe
    resumability and progress intelligence.
    """
    with connection:
        connection.execute(_INSERT_CHECKPOINT, row)


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
        connection.execute(_UPSERT_PROGRESS, row)


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