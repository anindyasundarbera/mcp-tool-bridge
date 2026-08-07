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


class StateStoreError(RuntimeError):
    """Raised when the durable state store cannot be opened or is unsupported."""


def ensure_state_dir(state_root: Path) -> Path:
    """Create and validate the private ``.state`` directory, returning it."""
    if not state_root.is_absolute():
        raise StateStoreError("state root must be absolute")
    return _ensure_private_directory(state_root)


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