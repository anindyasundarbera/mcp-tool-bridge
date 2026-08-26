"""Independent candidate review for the local development bridge.

A development job drafts a candidate in a detached, retained worktree.
``verification`` binds the candidate to a ``(base_head, fingerprint, paths)``
triple and runs only registry-configured commands — it is deliberately
syntactic. This module adds the missing *semantic* layer: a way to expose the
retained candidate to a SEPARATE reviewer (a fresh Claude session, or a human
recording a verdict through the bridge) without widening authorization, leaking
the worktree path, or letting the reviewer mutate anything.

Three opt-in layers, all dormant unless a project enables them:

* **Candidate read** (``CANDIDATE_REVIEW`` capability) — ``list_candidate_changes``
  and ``read_candidate_file`` read the retained worktree, scoped strictly to the
  candidate's changed-path set (the same set verification fingerprints), via the
  same ``_safe_path`` symlink/traversal discipline. Any external reviewer the
  operator chooses can inspect the candidate through the bridge.
* **Review records** (``CANDIDATE_REVIEW``) — ``record_candidate_review`` and
  ``run_independent_review`` bind a verdict to the exact candidate state, stored
  in an additive ``reviews`` table. A record is single-use: the optional commit
  gate consumes it once.
* **Bridge-spawned reviewer** (``allow_bridge_reviewer``) — ``run_independent_review``
  launches a fresh, read-only, non-resuming Claude session in the worktree, so the
  bridge itself enforces separation of duties (the reviewer provably is not the
  drafting session and cannot edit).

The module imports only *helpers* from ``verification`` (``_safe_path``,
``_candidate_state``, ``candidate_changed_paths``, ``_redact`` …) so review and
verification share one notion of "what the candidate changed" and one redaction
discipline. The dependency stays one-way: ``verification`` never imports
``review``, so the commit path is unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from . import store
from .claude import ClaudeExecutor
from .config import Settings
from .git import GitCommandError
from .jobs import DevelopmentJobError, DevelopmentJobManager, resolve_effort
from .models import Effort, ProjectPolicy
from .registry import ProjectLockManager
from .verification import (
    CandidateState,
    VerificationError,
    _bound_redacted_output,
    _candidate_state,
    _is_expired,
    _open_candidate_read,
    _redact,
    _safe_path,
    _validate_policy_path,
    candidate_changed_paths,
)
from .worktrees import GitWorktreeManager, JobWorktree, WorktreeError


class ReviewError(RuntimeError):
    """Raised when a candidate cannot be safely reviewed or a review is invalid."""


# A verdict is structured advice, not a file dump. Bound it so a reviewer cannot
# use the review record as an exfiltration channel.
MAX_REVIEW_VERDICT_CHARS = 20000
# Per-file candidate read bound (matches the default Markdown read bound).
MAX_CANDIDATE_READ_BYTES = 1_048_576


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _record_json_loads(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def compute_verdict_hash(
    verdict: dict[str, Any],
    base_head: str,
    diff_fingerprint: str,
    paths: tuple[str, ...] | list[str],
) -> str:
    """M-EV-04: content-address a review with a stable, deterministic hash.

    The hash is a 64-hex SHA-256 over a canonical JSON envelope of:

    * ``verdict`` — the structured verdict dict (re-serialized with
      ``sort_keys=True`` so key order never affects the hash);
    * ``base_head`` — the reviewed base HEAD;
    * ``diff_fingerprint`` — the candidate diff fingerprint;
    * ``paths`` — the candidate changed-path list, sorted lexicographically so
      a re-ordered path set (verification re-derives the set independently of
      order) hashes identically.

    The canonicalization is deterministic (``sort_keys=True``,
    ``ensure_ascii=False``) and bounded by ``MAX_REVIEW_VERDICT_CHARS`` (the
    verdict is already validated/bounded upstream). The hash changes when the
    verdict text changes or when any binding field (``base_head``,
    ``diff_fingerprint``, the sorted ``paths`` set) changes — so an
    orchestrator can detect drift without re-reading the verdict.
    """
    envelope = {
        "verdict": verdict,
        "base_head": base_head,
        "diff_fingerprint": diff_fingerprint,
        "paths": sorted(paths),
    }
    canonical = json.dumps(envelope, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ReviewRecord:
    review_id: str
    project_id: str
    job_id: str
    base_head: str
    diff_fingerprint: str
    paths: tuple[str, ...]
    reviewed_at: str
    reviewer_id: str | None
    verdict: dict[str, Any]
    used: bool = False
    # M-EV-03: evidence-binding fields, persisted in the ``review_bindings``
    # sidecar (not on ``reviews``) so the schema stays additive.
    # ``reviewer_independence_class`` is one of ``bridge_spawned|human|external``
    # — stamped by this module: ``bridge_spawned`` for ``run_independent_review``,
    # ``human`` (default) or ``external`` (caller-signaled) for
    # ``record_candidate_review``. ``toolchain_digest`` optionally carries the
    # M-EV-02 environment/toolchain digest. ``expires_at`` is an ISO-8601
    # timestamp (NULL means no expiry → always allowed at the commit gate).
    # ``authority_ref`` links to the ``authority_records`` from M-EV-05.
    reviewer_independence_class: str | None = None
    toolchain_digest: str | None = None
    expires_at: str | None = None
    authority_ref: str | None = None
    # M-EV-04: content-addressing hash, persisted in the ``review_content``
    # sidecar. ``verdict_hash`` is a 64-hex SHA-256 over a deterministic
    # canonicalization of the verdict text + the binding fields (``base_head``,
    # ``diff_fingerprint``, the sorted ``paths`` list). NULL until the review is
    # persisted; re-loaded from the sidecar on restart.
    verdict_hash: str | None = None


def review_to_row(record: ReviewRecord) -> dict[str, Any]:
    """Serialize a review record to a dict row for the durable store."""
    return {
        "review_id": record.review_id,
        "project_id": record.project_id,
        "job_id": record.job_id,
        "base_head": record.base_head,
        "diff_fingerprint": record.diff_fingerprint,
        "paths_json": json.dumps(list(record.paths), ensure_ascii=False),
        "reviewed_at": record.reviewed_at,
        "reviewer_id": record.reviewer_id,
        "verdict_json": json.dumps(record.verdict, ensure_ascii=False),
        "used": 1 if record.used else 0,
    }


def review_from_row(
    row: dict[str, Any],
    *,
    reviewer_independence_class: str | None = None,
    toolchain_digest: str | None = None,
    expires_at: str | None = None,
    authority_ref: str | None = None,
    verdict_hash: str | None = None,
) -> ReviewRecord | None:
    """Rebuild a review record from a dict row, or None if unparseable.

    M-EV-03: the optional binding fields come from the ``review_bindings``
    sidecar (loaded separately by the caller) so the record carries its
    evidence-binding envelope without the ``reviews`` table being altered.

    M-EV-04: ``verdict_hash`` comes from the ``review_content`` sidecar (loaded
    separately by the caller) so the record carries its content-addressing hash.
    """
    paths = _record_json_loads(row.get("paths_json"))
    verdict = _record_json_loads(row.get("verdict_json"))
    if not isinstance(paths, list) or not isinstance(verdict, dict):
        return None
    try:
        return ReviewRecord(
            review_id=row["review_id"],
            project_id=row["project_id"],
            job_id=row["job_id"],
            base_head=row["base_head"],
            diff_fingerprint=row["diff_fingerprint"],
            paths=tuple(paths),
            reviewed_at=row["reviewed_at"],
            reviewer_id=row.get("reviewer_id"),
            verdict=verdict,
            used=bool(row.get("used", 0)),
            reviewer_independence_class=reviewer_independence_class,
            toolchain_digest=toolchain_digest,
            expires_at=expires_at,
            authority_ref=authority_ref,
            verdict_hash=verdict_hash,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _normalize_review_path(value: str) -> str:
    """Normalize a single candidate path for membership comparison.

    Mirrors ``_decode_paths`` discipline but for one path: reject absolute,
    ``..``, empty, and non-canonical forms so a reviewer cannot reach outside the
    worktree or the candidate's changed-path set by normalization tricks.
    """
    normalized = value.replace("\\", "/").strip()
    parsed = PurePosixPath(normalized)
    if (
        not normalized
        or normalized == "."
        or parsed.is_absolute()
        or ".." in parsed.parts
        or normalized != parsed.as_posix()
    ):
        raise ReviewError(
            "path must be a normalized relative path without '.', '..', or empty segments"
        )
    return normalized


#: Reviewer id minted only by the bridge-spawned reviewer (``run_independent_review``).
#: It is reserved so a caller using ``record_candidate_review`` cannot forge a
#: record that looks indistinguishable from an attested independent review: an
#: operator inspecting ``reviewer_id == "bridge-reviewer"`` can trust it was set
#: by the bridge, not by the caller.
_BRIDGE_REVIEWER_ID = "bridge-reviewer"

# M-EV-03: the structured independence class that replaces/supplements the
# implicit ``_BRIDGE_REVIEWER_ID`` sentinel. ``run_independent_review`` stamps
# ``bridge_spawned``; ``record_candidate_review`` stamps ``human`` (default) or
# ``external`` when the caller signals it. The existing ``reviewer_id``
# sentinel keeps working for back-compat (additive — not removed).
REVIEWER_INDEPENDENCE_BRIDGE_SPAWNED = "bridge_spawned"
REVIEWER_INDEPENDENCE_HUMAN = "human"
REVIEWER_INDEPENDENCE_EXTERNAL = "external"
_REVIEWER_INDEPENDENCE_CLASSES = frozenset(
    {
        REVIEWER_INDEPENDENCE_BRIDGE_SPAWNED,
        REVIEWER_INDEPENDENCE_HUMAN,
        REVIEWER_INDEPENDENCE_EXTERNAL,
    }
)


def _validate_reviewer_id(reviewer_id: str | None) -> str | None:
    if reviewer_id is None:
        return None
    cleaned = reviewer_id.strip()
    if not cleaned:
        return None
    if cleaned == _BRIDGE_REVIEWER_ID:
        raise ReviewError(
            "reviewer_id 'bridge-reviewer' is reserved for the bridge-spawned reviewer"
        )
    if len(cleaned) > 200 or any(ord(character) < 32 for character in cleaned):
        raise ReviewError("reviewer_id must be at most 200 printable characters")
    return cleaned


def _coerce_verdict(verdict: Any) -> dict[str, Any]:
    """Validate and bound a caller-supplied verdict.

    A verdict must be a JSON-serializable dict with non-empty content. An empty
    verdict carries no review authority and is rejected so the commit gate cannot
    be satisfied by a no-op record.
    """
    if not isinstance(verdict, dict):
        raise ReviewError("verdict must be a structured object")
    try:
        encoded = json.dumps(verdict, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ReviewError("verdict must be JSON-serializable") from exc
    if len(encoded) > MAX_REVIEW_VERDICT_CHARS:
        raise ReviewError(
            f"verdict exceeds the {MAX_REVIEW_VERDICT_CHARS}-character limit"
        )
    if encoded in {"{}", "[]"} or not any(
        value
        for value in verdict.values()
        if isinstance(value, (str, list, dict)) and value
    ):
        raise ReviewError("verdict must not be empty")
    return verdict


def _candidate_file_metadata(worktree_path: Path, relative: str) -> dict[str, Any]:
    """Read bounded metadata for one candidate path (size, sha256, mode).

    Deletions (``_open_candidate_read`` returns None) are reported as ``deleted``
    with no content. Regular files at or under the read bound get a full content
    SHA-256; larger files are flagged ``exceeds_read_limit`` and not hashed, so
    listing never reads an unbounded amount. Symlinks and non-regular types are
    rejected by ``_open_candidate_read``.
    """
    file_descriptor = _open_candidate_read(worktree_path, relative, allow_missing=True)
    if file_descriptor is None:
        return {"path": relative, "deleted": True, "size_bytes": 0}
    try:
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ReviewError("candidate paths must be regular files or deletions")
        size = metadata.st_size
        entry: dict[str, Any] = {
            "path": relative,
            "deleted": False,
            "size_bytes": size,
            "executable": bool(metadata.st_mode & 0o111),
        }
        if size <= MAX_CANDIDATE_READ_BYTES:
            remaining = size
            digest = hashlib.sha256()
            while remaining > 0:
                chunk = os.read(file_descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
            entry["sha256"] = digest.hexdigest()
        else:
            entry["exceeds_read_limit"] = True
        return entry
    except OSError as exc:
        raise ReviewError(f"candidate file could not be inspected: {exc}") from exc
    finally:
        os.close(file_descriptor)


def _read_candidate_bytes(worktree_path: Path, relative: str) -> tuple[bytes, dict[str, Any]]:
    """Read one candidate file's full bytes safely, with metadata.

    Raises ``ReviewError`` if the file is deleted, is not regular, exceeds the
    read bound, or changed while being read. Returns ``(bytes, metadata)`` where
    metadata carries size, sha256, and executable flag.
    """
    file_descriptor = _open_candidate_read(worktree_path, relative, allow_missing=False)
    assert file_descriptor is not None  # allow_missing=False guarantees a regular file
    try:
        before = os.fstat(file_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ReviewError("candidate paths must be regular files or deletions")
        if before.st_size > MAX_CANDIDATE_READ_BYTES:
            raise ReviewError(
                f"candidate file exceeds the {MAX_CANDIDATE_READ_BYTES}-byte read limit"
            )
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        while total < before.st_size:
            chunk = os.read(file_descriptor, min(1024 * 1024, before.st_size - total))
            if not chunk:
                break
            chunks.append(chunk)
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(file_descriptor)
        if (
            total != before.st_size
            or after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
        ):
            raise ReviewError("candidate file changed while it was read")
        data = b"".join(chunks)
        return data, {
            "size_bytes": before.st_size,
            "sha256": digest.hexdigest(),
            "executable": bool(before.st_mode & 0o111),
        }
    except OSError as exc:
        raise ReviewError(f"candidate file could not be read: {exc}") from exc
    finally:
        os.close(file_descriptor)


class ReviewManager:
    def __init__(
        self,
        settings: Settings,
        locks: ProjectLockManager,
        jobs: DevelopmentJobManager,
        executor: ClaudeExecutor,
        worktrees: GitWorktreeManager | None = None,
        projects: tuple[ProjectPolicy, ...] = (),
    ) -> None:
        self.settings = settings
        self._locks = locks
        self._jobs = jobs
        self._executor = executor
        self._worktrees = worktrees or GitWorktreeManager(
            settings, tuple(project.root for project in projects)
        )
        self._project_roots = tuple(project.root for project in projects)
        self._reviews: dict[str, ReviewRecord] = {}
        self._reviews_lock = asyncio.Lock()
        # Durable state: re-hydrate review records from the same database the
        # jobs and verification managers use. The ``reviews`` table is additive
        # (no schema-version bump), so an older code path ignores it. WAL
        # serializes the third writer safely.
        self._db = store.open_state_db(self._worktrees.ensure_state_root() / "bridge.db")
        self._reload_reviews()

    def _reload_reviews(self) -> None:
        """Load persisted review records, skipping unparseable rows.

        M-EV-03: also loads the ``review_bindings`` sidecar so each
        re-hydrated record carries its ``reviewer_independence_class``,
        ``toolchain_digest``, ``expires_at``, and ``authority_ref`` envelope.

        M-EV-04: also loads the ``review_content`` sidecar so each re-hydrated
        record carries its ``verdict_hash`` (content-addressing hash).
        """
        bindings = {
            row["review_id"]: row for row in store.load_review_binding_rows(self._db)
        }
        contents = {
            row["review_id"]: row for row in store.load_review_content_rows(self._db)
        }
        for row in store.load_review_rows(self._db):
            review_id = row["review_id"]
            binding = bindings.get(review_id)
            content = contents.get(review_id)
            record = review_from_row(
                row,
                reviewer_independence_class=(
                    binding.get("reviewer_independence_class") if binding else None
                ),
                toolchain_digest=binding.get("toolchain_digest") if binding else None,
                expires_at=binding.get("expires_at") if binding else None,
                authority_ref=binding.get("authority_ref") if binding else None,
                verdict_hash=(
                    content.get("verdict_hash") if content else None
                ),
            )
            if record is not None:
                self._reviews[record.review_id] = record

    def _persist_review(self, record: ReviewRecord) -> None:
        """Persist one review record as a single ACID transaction.

        Call this inside ``async with self._reviews_lock`` after mutating a
        record so the durable store mirrors the in-memory state.

        M-EV-03: also upserts the ``review_bindings`` sidecar row so the
        record's evidence-binding envelope (``reviewer_independence_class``,
        ``toolchain_digest``, ``expires_at``, ``authority_ref``) is durable.
        The binding is always written (even when the fields are NULL) so a
        reload sees the binding envelope exists; the commit-gate expiry check
        reads it back.

        M-EV-04: also upserts the ``review_content`` sidecar row so the
        record's ``verdict_hash`` (content-addressing hash over the verdict text
        + binding fields) is durable. The hash is (re)computed here from the
        record's current fields so a mutated verdict or binding set is
        re-hashed on every persist — the sidecar is always in sync with the
        in-memory record. The hash is also stamped back onto the record so the
        caller sees it without a reload.
        """
        record.verdict_hash = compute_verdict_hash(
            record.verdict,
            record.base_head,
            record.diff_fingerprint,
            record.paths,
        )
        store.upsert_review_row(self._db, review_to_row(record))
        store.upsert_review_binding_row(
            self._db,
            {
                "review_id": record.review_id,
                "toolchain_digest": record.toolchain_digest,
                "reviewer_independence_class": record.reviewer_independence_class,
                "expires_at": record.expires_at,
                "authority_ref": record.authority_ref,
            },
        )
        store.upsert_review_content_row(
            self._db,
            {
                "review_id": record.review_id,
                "verdict_hash": record.verdict_hash,
            },
        )

    def _new_review_id(self) -> str:
        for _ in range(10):
            candidate = secrets.token_hex(16)
            if candidate not in self._reviews and not store.review_id_exists(
                self._db, candidate
            ):
                return candidate
        raise ReviewError("could not allocate a unique review ID")

    def _redact_paths(
        self, project: ProjectPolicy, worktree: JobWorktree
    ) -> tuple[Path, ...]:
        return (
            worktree.path,
            project.root,
            self.settings.job_worktree_root,
            self.settings.projects_config_path,
            *self._project_roots,
        )

    def _redact_paths_for_job(
        self, project: ProjectPolicy, job_id: str
    ) -> tuple[Path, ...]:
        """Redact paths for a read-only review lookup, without the project lock.

        ``get_candidate_review`` deliberately does not resolve the worktree
        object (it takes only the reviews lock), but the retained worktree lives
        at the deterministic ``job_worktree_root / project_id / job_id`` (see
        ``GitWorktreeManager.create``). Include that path so a verdict that
        echoes the reviewer's or drafter's cwd — an absolute worktree path — is
        still redacted even though no worktree is resolved here.
        """
        worktree_path = self.settings.job_worktree_root / project.id / job_id
        return (
            worktree_path,
            project.root,
            self.settings.job_worktree_root,
            self.settings.projects_config_path,
            *self._project_roots,
        )

    async def _reviewable_worktree_locked(
        self, project: ProjectPolicy, job_id: str
    ) -> JobWorktree:
        """Return a validated retained worktree (caller holds the project lock).

        Reuses the verification entry preconditions exactly: only a completed,
        inactive, uncommitted job with a surviving worktree is reviewable, so
        review and verification always see the same candidate. ``Development`` /
        ``worktree`` / ``git`` failures are normalized to ``ReviewError``.
        """
        try:
            worktree = await self._jobs.verification_worktree(project, job_id)
            await self._worktrees.validate_resume(project, worktree)
            return worktree
        except (DevelopmentJobError, WorktreeError, GitCommandError) as exc:
            raise ReviewError(str(exc)) from exc

    async def list_candidate_changes(
        self,
        project: ProjectPolicy,
        job_id: str,
    ) -> dict[str, Any]:
        """List the candidate's changed paths with bounded, redacted metadata.

        Read-only. Does not require an empty index (review may inspect a
        candidate with staged changes). The path set is the same one
        verification would fingerprint, so a reviewer and a verifier agree on
        what changed.
        """
        lock = await self._locks.for_project(project.id)
        async with lock:
            worktree = await self._reviewable_worktree_locked(project, job_id)
            try:
                paths = await candidate_changed_paths(self.settings, worktree)
                entries: list[dict[str, Any]] = []
                for relative in paths:
                    _validate_policy_path(project, relative)
                    entries.append(
                        _redact(
                            _candidate_file_metadata(worktree.path, relative),
                            self._redact_paths(project, worktree),
                        )
                    )
                return {
                    "project_id": project.id,
                    "job_id": job_id,
                    "base_head": worktree.base_head,
                    "paths": entries,
                }
            except VerificationError as exc:
                raise ReviewError(str(exc)) from exc

    async def read_candidate_file(
        self,
        project: ProjectPolicy,
        job_id: str,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Read one candidate file, scoped to the candidate's changed-path set.

        ``path`` must be one of the candidate's changed paths (re-derived fresh
        from the worktree) and must pass the same policy checks verification
        applies, so a reviewer cannot traverse outside the candidate. Supports
        an optional bounded line range and an optional SHA-256 consistency
        check, mirroring the Markdown reader. Deletions are reported without
        content.
        """
        relative = _normalize_review_path(path)
        if start_line is not None or end_line is not None:
            if start_line is None or end_line is None:
                raise ReviewError("start_line and end_line must be supplied together")
            if start_line < 1 or end_line < start_line:
                raise ReviewError(
                    "line range must satisfy 1 <= start_line <= end_line"
                )
            if end_line - start_line + 1 > self.settings.max_markdown_range_lines:
                raise ReviewError(
                    "requested line range exceeds the configured limit"
                )

        lock = await self._locks.for_project(project.id)
        async with lock:
            worktree = await self._reviewable_worktree_locked(project, job_id)
            try:
                paths = await candidate_changed_paths(self.settings, worktree)
                if relative not in paths:
                    raise ReviewError("path is not part of the candidate changes")
                _validate_policy_path(project, relative)
                target = _safe_path(worktree.path, relative, allow_missing=True)
                base: dict[str, Any] = {
                    "project_id": project.id,
                    "job_id": job_id,
                    "path": relative,
                    "base_head": worktree.base_head,
                }
                if target is None:
                    # A deletion has no content to read; report it as such.
                    return _redact(
                        {**base, "deleted": True}, self._redact_paths(project, worktree)
                    )
                data, metadata = _read_candidate_bytes(worktree.path, relative)
                digest = metadata["sha256"]
                if expected_sha256 is not None and expected_sha256.lower() != digest:
                    raise ReviewError(
                        "expected_sha256 does not match the current candidate file"
                    )
                response: dict[str, Any] = {
                    **base,
                    "deleted": False,
                    "sha256": digest,
                    "size_bytes": metadata["size_bytes"],
                    "executable": metadata["executable"],
                }
                text = data.decode("utf-8", errors="replace")
                if start_line is not None:
                    lines = text.splitlines(keepends=True)
                    if start_line > len(lines):
                        raise ReviewError("start_line is beyond the end of the file")
                    actual_end = min(end_line, len(lines))
                    response["total_lines"] = len(lines)
                    response["start_line"] = start_line
                    response["end_line"] = actual_end
                    response["content"] = "".join(lines[start_line - 1 : actual_end])
                else:
                    response["content"] = text
                return _redact(response, self._redact_paths(project, worktree))
            except VerificationError as exc:
                raise ReviewError(str(exc)) from exc

    async def record_candidate_review(
        self,
        project: ProjectPolicy,
        job_id: str,
        verdict: Any,
        reviewer_id: str | None = None,
        *,
        external: bool = False,
        expires_at: str | None = None,
        authority_ref: str | None = None,
    ) -> dict[str, Any]:
        """Bind a caller-supplied verdict to the exact current candidate state.

        The verdict is validated and bounded, then persisted as a single-use
        review record keyed to the same ``(base_head, fingerprint, paths)`` triple
        verification uses, so the optional commit gate can match it to a
        verification record.

        Trust boundary: the bridge records the verdict the caller passes and
        binds it to the candidate state, but it does **not** attest *who*
        reviewed — any client with ``CANDIDATE_REVIEW`` can call this, including
        the drafting agent, so a drafter could self-review and satisfy a
        ``requires_review`` gate. This tool is the route for an *external*
        (human or out-of-band) reviewer to lodge a verdict through the bridge;
        the strong, attested guarantee of an independent reviewer is
        ``run_independent_review``, which the bridge spawns itself.

        M-EV-03: stamps ``reviewer_independence_class`` on the review binding —
        ``human`` by default, or ``external`` when the caller signals it via
        ``external=True``. The existing ``reviewer_id`` sentinel keeps working
        for back-compat. ``expires_at`` (ISO-8601) and ``authority_ref`` are
        optional evidence-binding envelope fields, persisted in the
        ``review_bindings`` sidecar.
        """
        coerced = _coerce_verdict(verdict)
        reviewer = _validate_reviewer_id(reviewer_id)
        independence_class = (
            REVIEWER_INDEPENDENCE_EXTERNAL
            if external
            else REVIEWER_INDEPENDENCE_HUMAN
        )
        if expires_at is not None:
            try:
                datetime.fromisoformat(expires_at)
            except ValueError as exc:
                raise ReviewError("expires_at must be an ISO-8601 timestamp") from exc
        lock = await self._locks.for_project(project.id)
        async with lock:
            worktree = await self._reviewable_worktree_locked(project, job_id)
            try:
                state = await _candidate_state(self.settings, project, worktree)
            except VerificationError as exc:
                raise ReviewError(str(exc)) from exc
            async with self._reviews_lock:
                record = ReviewRecord(
                    review_id=self._new_review_id(),
                    project_id=project.id,
                    job_id=job_id,
                    base_head=state.base_head,
                    diff_fingerprint=state.fingerprint,
                    paths=state.paths,
                    reviewed_at=_timestamp(),
                    reviewer_id=reviewer,
                    verdict=coerced,
                    reviewer_independence_class=independence_class,
                    expires_at=expires_at,
                    authority_ref=authority_ref,
                )
                self._reviews[record.review_id] = record
                self._persist_review(record)
            return _redact(
                {
                    "status": "recorded",
                    "project_id": project.id,
                    "job_id": job_id,
                    "review_id": record.review_id,
                    "base_head": record.base_head,
                    "diff_fingerprint": record.diff_fingerprint,
                    "paths": list(record.paths),
                    "reviewed_at": record.reviewed_at,
                    "reviewer_id": record.reviewer_id,
                    "reviewer_independence_class": record.reviewer_independence_class,
                    # M-EV-04: content-addressing hash over the verdict + bindings.
                    "verdict_hash": record.verdict_hash,
                },
                self._redact_paths(project, worktree),
            )

    async def run_independent_review(
        self,
        project: ProjectPolicy,
        job_id: str,
        effort: Effort | None = None,
    ) -> dict[str, Any]:
        """Spawn a fresh, read-only Claude reviewer over the candidate.

        Requires ``allow_bridge_reviewer``. The reviewer is launched with
        ``build_reviewer_command`` (no ``--resume``, only ``Read,Glob,Grep``), so
        it is provably a distinct session from the drafting agent and cannot
        mutate the candidate. The candidate state is bound before and re-checked
        after the review; any drift rejects the review. The bounded verdict is
        persisted as a single-use review record.
        """
        if not project.allow_bridge_reviewer:
            raise ReviewError("project does not allow a bridge-spawned reviewer")
        effort = resolve_effort(project, effort, self.settings)
        lock = await self._locks.for_project(project.id)
        async with lock:
            worktree = await self._reviewable_worktree_locked(project, job_id)
            try:
                before = await _candidate_state(self.settings, project, worktree)
            except VerificationError as exc:
                raise ReviewError(str(exc)) from exc
            specification = self._build_review_spec(project, before)
            result = await self._executor.execute_review(
                project,
                specification,
                effort,
                root=worktree.path,
            )
            try:
                after = await _candidate_state(self.settings, project, worktree)
            except VerificationError as exc:
                raise ReviewError(str(exc)) from exc
            if after != before:
                raise ReviewError("candidate changed during independent review")
            if result.get("status") != "completed":
                detail = (
                    result.get("error") or result.get("status") or "review failed"
                )
                raise ReviewError(f"independent review did not complete: {detail}")
            verdict = self._verdict_from_result(result)
            _coerce_verdict(verdict)  # bound + non-empty check
            async with self._reviews_lock:
                record = ReviewRecord(
                    review_id=self._new_review_id(),
                    project_id=project.id,
                    job_id=job_id,
                    base_head=before.base_head,
                    diff_fingerprint=before.fingerprint,
                    paths=before.paths,
                    reviewed_at=_timestamp(),
                    reviewer_id=_BRIDGE_REVIEWER_ID,
                    verdict=verdict,
                    # M-EV-03: the bridge stamps the structured independence
                    # class for a bridge-spawned review. The existing
                    # ``_BRIDGE_REVIEWER_ID`` sentinel keeps working for
                    # back-compat (additive — not removed).
                    reviewer_independence_class=(
                        REVIEWER_INDEPENDENCE_BRIDGE_SPAWNED
                    ),
                )
                self._reviews[record.review_id] = record
                self._persist_review(record)
            redacted_result = _bound_redacted_output(
                _redact(result, self._redact_paths(project, worktree)),
                self.settings.max_verification_output_bytes,
            )
            return {
                "status": "recorded",
                "project_id": project.id,
                "job_id": job_id,
                "review_id": record.review_id,
                "base_head": record.base_head,
                "diff_fingerprint": record.diff_fingerprint,
                "paths": list(record.paths),
                "reviewed_at": record.reviewed_at,
                "reviewer_id": record.reviewer_id,
                "reviewer_independence_class": record.reviewer_independence_class,
                # M-EV-04: content-addressing hash over the verdict + bindings.
                "verdict_hash": record.verdict_hash,
                "review": redacted_result,
            }

    def _build_review_spec(self, project: ProjectPolicy, state: CandidateState) -> str:
        paths_block = "\n".join(state.paths)
        return (
            "You are an independent reviewer. A separate drafting agent produced "
            "the candidate changes below in an isolated worktree. Your job is to "
            "review them, NOT to modify anything (you have read-only tools).\n\n"
            f"Project: {project.id}\n"
            f"Base HEAD: {state.base_head}\n"
            f"Changed paths ({len(state.paths)}):\n{paths_block}\n\n"
            "Read each changed path, then assess correctness, completeness, hidden "
            "risks, and whether the changes match their stated intent. Return a "
            "concise structured verdict as JSON with keys \"summary\" (string), "
            "\"issues\" (list of strings), and \"recommendation\" (string)."
        )

    def _verdict_from_result(self, result: dict[str, Any]) -> dict[str, Any]:
        text = str(result.get("result", ""))
        parsed: dict[str, Any] | None = None
        if text:
            try:
                decoded = json.loads(text)
                if isinstance(decoded, dict):
                    parsed = decoded
            except (json.JSONDecodeError, TypeError):
                parsed = None
        if parsed is not None:
            return parsed
        # Fall back to a structured wrapper so a non-JSON verdict is still bounded
        # and clearly labelled, never an empty record.
        return {
            "summary": text[:MAX_REVIEW_VERDICT_CHARS] or "no verdict text",
            "issues": [],
            "recommendation": "",
            "raw": text[:MAX_REVIEW_VERDICT_CHARS],
        }

    async def get_candidate_review(
        self,
        project: ProjectPolicy,
        job_id: str,
        review_id: str,
    ) -> dict[str, Any]:
        """Return a stored review verdict (read-only; does not take the project lock).

        The verdict is redacted: it is caller- or reviewer-supplied structured
        advice, so the whole response is passed through ``_redact`` (over the
        worktree path, project root, ``job_worktree_root``, config path, and all
        project roots) before it leaves the bridge. Used to surface recorded
        review evidence without re-running a reviewer.
        """
        async with self._reviews_lock:
            record = self._reviews.get(review_id)
            if (
                record is None
                or record.project_id != project.id
                or record.job_id != job_id
            ):
                raise ReviewError("unknown review_id for this project and job")
            return _redact(
                {
                    "project_id": record.project_id,
                    "job_id": record.job_id,
                    "review_id": record.review_id,
                    "base_head": record.base_head,
                    "diff_fingerprint": record.diff_fingerprint,
                    "paths": list(record.paths),
                    "reviewed_at": record.reviewed_at,
                    "reviewer_id": record.reviewer_id,
                    "used": record.used,
                    "verdict": record.verdict,
                    # M-EV-04: content-addressing hash over the verdict + bindings.
                    "verdict_hash": record.verdict_hash,
                },
                self._redact_paths_for_job(project, job_id),
            )

    def get_review_closure_chain(self, review_id: str) -> dict[str, Any]:
        """M-EV-04: return the content-address + finding->revision->closure chain.

        Given a ``review_id``, returns ``{"review_id", "verdict_hash",
        "resolutions": [...]}`` where each resolution is a
        ``{"finding_ref", "resolving_job_id", "resolving_checkpoint_id",
        "resolved_at"}`` row from the ``revision_resolves_finding`` sidecar.
        Raises ``ReviewError`` if the review is unknown. Read-only; takes no
        lock (it reads durable sidecar rows that are append-only). Internal —
        no new MCP tool; an introspection/snapshot path may surface it.
        """
        if review_id not in self._reviews and not store.review_id_exists(
            self._db, review_id
        ):
            raise ReviewError("unknown review_id")
        content = store.load_review_content_row(self._db, review_id)
        verdict_hash = content.get("verdict_hash") if content else None
        rows = store.load_revision_resolves_finding_rows_for_review(
            self._db, review_id
        )
        resolutions = [
            {
                "finding_ref": row.get("finding_ref"),
                "resolving_job_id": row.get("resolving_job_id"),
                "resolving_checkpoint_id": row.get("resolving_checkpoint_id"),
                "resolved_at": row.get("resolved_at"),
            }
            for row in rows
        ]
        return {
            "review_id": review_id,
            "verdict_hash": verdict_hash,
            "resolutions": resolutions,
        }

    async def validate_for_commit(
        self,
        project: ProjectPolicy,
        job_id: str,
        review_id: str,
        expected_head: str,
        expected_diff_fingerprint: str,
        paths: list[str],
    ) -> ReviewRecord:
        """Validate that a review record authorizes committing this exact state.

        Called by the commit tool when ``requires_review`` is set, BEFORE the
        commit runs. Matches the review record against the verification evidence
        the caller is about to commit: same base HEAD, same fingerprint, same
        ordered path list. Rejects an already-used review (single-use). Takes the
        review lock (not the project lock) so it composes with verification's own
        project-lock acquisition without deadlock.
        """
        async with self._reviews_lock:
            record = self._reviews.get(review_id)
            if (
                record is None
                or record.project_id != project.id
                or record.job_id != job_id
            ):
                raise ReviewError("unknown review_id for this project and job")
            if record.used:
                raise ReviewError("review_id has already been used")
            if expected_head != record.base_head:
                raise ReviewError("expected_head does not match reviewed base HEAD")
            if expected_diff_fingerprint != record.diff_fingerprint:
                raise ReviewError(
                    "expected_diff_fingerprint does not match review evidence"
                )
            if tuple(paths) != record.paths or len(paths) != len(set(paths)):
                raise ReviewError(
                    "paths must exactly match the reviewed ordered path list"
                )
            # M-EV-03: reject a review binding whose expiry has passed. A
            # NULL/absent ``expires_at`` means no expiry → allowed.
            if _is_expired(record.expires_at):
                raise ReviewError("review evidence binding has expired")
            return record

    @staticmethod
    def _verdict_recommendation(verdict: dict[str, Any]) -> str:
        """Return a normalized pass/fail recommendation from a verdict.

        A verdict's ``recommendation`` field is the reviewer's pass/fail signal.
        ``approve`` / ``pass`` / ``approved`` / ``accept`` are treated as a pass;
        anything else (``reject``, ``changes``, empty, etc.) is treated as a
        fail. Unknown or missing recommendations fail closed (no pass).
        """
        raw = verdict.get("recommendation")
        if not isinstance(raw, str):
            return "fail"
        normalized = raw.strip().lower()
        if normalized in {"approve", "pass", "approved", "accept", "accepted"}:
            return "pass"
        return "fail"

    async def reconcile_reviews(
        self,
        project: ProjectPolicy,
        job_id: str,
        review_ids: list[str],
    ) -> dict[str, Any]:
        """Reconcile a set of reviews for one job's candidate.

        Given the review records for a candidate, this determines whether the
        reviews agree (all pass or all fail) or diverge. On divergence it
        requires a tie-breaker: a third review with a distinct ``reviewer_id``
        that breaks the tie. Returns a structured reconciliation result with the
        resolved verdict. The commit gate requires a reconciled pass.
        """
        if len(review_ids) < 2:
            raise ReviewError("reconcile_reviews requires at least two reviews")
        async with self._reviews_lock:
            records: list[ReviewRecord] = []
            for review_id in review_ids:
                record = self._reviews.get(review_id)
                if (
                    record is None
                    or record.project_id != project.id
                    or record.job_id != job_id
                ):
                    raise ReviewError(
                        f"unknown review_id for this project and job: {review_id}"
                    )
                records.append(record)

            recommendations = [
                self._verdict_recommendation(record.verdict) for record in records
            ]
            passes = recommendations.count("pass")
            fails = len(recommendations) - passes

            if passes == len(recommendations):
                resolved = "pass"
            elif fails == len(recommendations):
                resolved = "fail"
            else:
                # Divergent verdicts: require a bridge-attested tie-breaker.
                # Caller-supplied ``reviewer_id`` values are self-reported and
                # unauthenticated, so distinct labels are NOT evidence of
                # distinct reviewers — a single actor could forge three labels.
                # Only a review produced by ``run_independent_review`` (which
                # the bridge itself spawns with a fresh read-only session) is
                # attested, so the tie-breaker must be one of those.
                attested = [
                    record
                    for record in records
                    if record.reviewer_id == _BRIDGE_REVIEWER_ID
                ]
                if not attested:
                    raise ReviewError(
                        "reviews diverge; a bridge-attested tie-breaker review "
                        "(run_independent_review) is required to reconcile"
                    )
                resolved = "pass" if passes > fails else "fail"

            return {
                "project_id": project.id,
                "job_id": job_id,
                "review_ids": list(review_ids),
                "recommendations": recommendations,
                "resolved": resolved,
                "reconciled": True,
            }

    async def mark_used(self, review_id: str) -> None:
        """Mark a review record consumed by a successful commit.

        Called AFTER the commit succeeds. A review is single-use per successful
        commit; verification's single-use ``verification_id`` is the true
        authority, so a commit can only succeed once regardless of review state.
        Atomic: the in-memory ``used`` flag flips only after the durable row is
        written. A persistence failure reverts the flag and re-raises, so memory
        and disk never diverge and the review stays reusable — the already-used
        ``verification_id`` still prevents a second commit.
        """
        async with self._reviews_lock:
            record = self._reviews.get(review_id)
            if record is None or record.used:
                return
            record.used = True
            try:
                self._persist_review(record)
            except Exception:
                record.used = False
                raise